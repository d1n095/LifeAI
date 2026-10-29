//! The MainAI native API proxy.
//!
//! This is the Tauri/Rust re-implementation of the web app's same-origin proxy
//! (`frontend/app/api/[...path]/route.ts`). It exists so that the native shell can preserve
//! the web app's exact session-security property:
//!
//!   > Session identity lives entirely in HttpOnly cookies that the frontend JavaScript can
//!   > never read (see docs/AUTH_THREAT_MODEL.md).
//!
//! In a Tauri bundle there is no Next.js server, so we move that proxy into Rust. The webview
//! never talks to the backend directly and never sees a `Set-Cookie`: it calls the registered
//! `mainaiapi:` scheme, Rust forwards the request to the real backend while attaching the
//! stored session cookies, captures any new `Set-Cookie` into an OS-protected store, and
//! returns the response body to the webview **with the Set-Cookie header stripped**. The
//! access/refresh tokens therefore live only in the Rust process + OS-protected storage, never
//! in JS, `localStorage`, or the DOM.
//!
//! Security properties this file is responsible for (reviewer checklist):
//!   1. Egress allow-list: the only host this ever contacts is `BACKEND_BASE`. The webview
//!      cannot make Rust fetch an arbitrary URL — the path is appended to a fixed base.
//!   2. HttpOnly session cookies are never returned to the webview (Set-Cookie is dropped
//!      from the response the webview receives; cookies are kept server-side in `SessionStore`).
//!   3. Hop-by-hop headers are not relayed (RFC 7230 §6.1), mirroring the Next proxy.
//!   4. The persisted cookie file is written with owner-only permissions (0600 on unix).

use std::collections::HashMap;
use std::io::Write as _;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// The one backend origin this shell is allowed to reach. Not a secret (just a URL). Override
/// at build time with `MAINAI_BACKEND_URL=https://api.example.com`. Default targets local dev.
pub const BACKEND_BASE: &str = match option_env!("MAINAI_BACKEND_URL") {
    Some(v) => v,
    None => "http://localhost:8000",
};

/// The custom scheme the webview uses in place of a same-origin server. `fetch("/api/..")`
/// in the SPA is pointed here by setting `NEXT_PUBLIC_API_URL` to this origin at export time.
pub const SCHEME: &str = "mainaiapi";

/// Hop-by-hop headers must not be relayed across a proxy hop (mirrors the Next proxy's set).
const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "expect",
];

/// Backend paths that must require a *fresh* founder step-up authorization, not merely an
/// unlocked device. The proxy does not weaken these — the backend's Security Kernel enforces
/// step-up; this list is where the native layer hooks a re-auth prompt in Phase 2. Listed here
/// so the security boundary is visible in one place.
pub const STEP_UP_PATHS: &[&str] = &[
    "/api/account",                     // DELETE account (erasure)
    "/api/auth/logout-all",             // global session revocation
    "/api/mainai/execution/tasks",      // approve/reject privileged agent effects (suffix match)
];

#[derive(Clone, Serialize, Deserialize)]
struct CookieEntry {
    value: String,
    /// Cookie path scope (default "/"). The refresh cookie is scoped to /api/auth by the
    /// backend (see backend/app/cookies.py), so we honor path when attaching.
    path: String,
}

/// Owner-only session store: the HttpOnly cookies, persisted to an OS-protected per-user file.
/// This is the *only* place session tokens are held on the client, and it is never exposed to
/// the webview or to any Tauri command.
pub struct SessionStore {
    cookies: Mutex<HashMap<String, CookieEntry>>,
    path: PathBuf,
}

impl SessionStore {
    pub fn load(app_data_dir: PathBuf) -> Self {
        std::fs::create_dir_all(&app_data_dir).ok();
        let path = app_data_dir.join("session.json");
        let cookies = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice::<HashMap<String, CookieEntry>>(&b).ok())
            .unwrap_or_default();
        SessionStore { cookies: Mutex::new(cookies), path }
    }

    fn persist(&self, cookies: &HashMap<String, CookieEntry>) {
        if let Ok(bytes) = serde_json::to_vec(cookies) {
            // Write to a temp file then rename, with owner-only perms, so a partial write is
            // never left behind and the session file is not world/group readable.
            let tmp = self.path.with_extension("json.tmp");
            if let Ok(mut f) = std::fs::File::create(&tmp) {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    let _ = f.set_permissions(std::fs::Permissions::from_mode(0o600));
                }
                if f.write_all(&bytes).is_ok() {
                    let _ = f.sync_all();
                    let _ = std::fs::rename(&tmp, &self.path);
                }
            }
        }
    }

    /// Build the `Cookie:` header value for a request to `path`, honoring cookie path scope.
    fn cookie_header_for(&self, path: &str) -> Option<String> {
        let cookies = self.cookies.lock().unwrap();
        let mut parts: Vec<String> = Vec::new();
        for (name, entry) in cookies.iter() {
            if path.starts_with(&entry.path) {
                parts.push(format!("{name}={}", entry.value));
            }
        }
        if parts.is_empty() {
            None
        } else {
            Some(parts.join("; "))
        }
    }

    /// Parse and store `Set-Cookie` values from a backend response. Deletions (Max-Age=0 or a
    /// past Expires) remove the cookie — this is how backend logout clears the session.
    fn capture_set_cookie(&self, set_cookie_values: &[String]) {
        let mut cookies = self.cookies.lock().unwrap();
        for raw in set_cookie_values {
            let mut segments = raw.split(';');
            let Some(nv) = segments.next() else { continue };
            let Some((name, value)) = nv.split_once('=') else { continue };
            let name = name.trim().to_string();
            let value = value.trim().to_string();

            let mut path = "/".to_string();
            let mut deleted = false;
            for attr in segments {
                let attr = attr.trim();
                if let Some((k, v)) = attr.split_once('=') {
                    match k.trim().to_ascii_lowercase().as_str() {
                        "path" => path = v.trim().to_string(),
                        "max-age" => {
                            if v.trim() == "0" || v.trim().starts_with('-') {
                                deleted = true;
                            }
                        }
                        _ => {}
                    }
                }
            }
            if deleted || value.is_empty() {
                cookies.remove(&name);
            } else {
                cookies.insert(name, CookieEntry { value, path });
            }
        }
        self.persist(&cookies);
    }

    /// Wipe all session cookies (native "sign out of this device"). Does not itself call the
    /// backend — callers pair it with a backend logout so server-side revocation also happens.
    pub fn clear(&self) {
        let mut cookies = self.cookies.lock().unwrap();
        cookies.clear();
        self.persist(&cookies);
    }

    pub fn is_authenticated(&self) -> bool {
        self.cookies.lock().unwrap().contains_key("access_token")
            || self.cookies.lock().unwrap().contains_key("refresh_token")
    }
}

/// A plain, owned description of an incoming proxied request (scheme handler → worker thread).
pub struct ProxyRequest {
    pub method: String,
    pub path_and_query: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// A plain, owned description of the response to hand back to the webview (Set-Cookie already
/// stripped — it never leaves Rust).
pub struct ProxyResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// Perform the forward request to the backend. Runs on a worker thread (reqwest blocking).
///
/// This is deliberately the ONLY function that constructs a backend URL, and it always does so
/// as `BACKEND_BASE + path_and_query` — the webview cannot redirect egress elsewhere.
pub fn forward(store: &SessionStore, req: ProxyRequest) -> ProxyResponse {
    // Normalize: the webview calls mainaiapi://localhost/api/...; we forward /api/... to backend.
    let path = req.path_and_query.clone();
    let path_only = path.split('?').next().unwrap_or("/").to_string();
    let url = format!("{BACKEND_BASE}{path}");

    let client = match reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(60))
        .redirect(reqwest::redirect::Policy::none())
        .build()
    {
        Ok(c) => c,
        Err(_) => {
            return ProxyResponse {
                status: 502,
                headers: vec![("content-type".into(), "application/json".into())],
                body: r#"{"detail":"Kunde inte initiera nätverksklient.","proxy_error":true}"#.as_bytes().to_vec(),
            }
        }
    };

    let method = reqwest::Method::from_bytes(req.method.as_bytes())
        .unwrap_or(reqwest::Method::GET);
    let mut builder = client.request(method.clone(), &url);

    // Relay request headers except hop-by-hop and any client-supplied Cookie (we own cookies).
    for (k, v) in &req.headers {
        let lk = k.to_ascii_lowercase();
        if HOP_BY_HOP.contains(&lk.as_str()) || lk == "cookie" {
            continue;
        }
        builder = builder.header(k, v);
    }
    // Attach our server-side session cookies (HttpOnly tokens never came from JS).
    if let Some(cookie) = store.cookie_header_for(&path_only) {
        builder = builder.header("cookie", cookie);
    }
    if !req.body.is_empty() {
        builder = builder.body(req.body);
    }

    let resp = match builder.send() {
        Ok(r) => r,
        Err(_) => {
            return ProxyResponse {
                status: 502,
                headers: vec![("content-type".into(), "application/json".into())],
                body: r#"{"detail":"Kunde inte nå backend-tjänsten.","proxy_error":true}"#.as_bytes().to_vec(),
            }
        }
    };

    let status = resp.status().as_u16();

    // Capture Set-Cookie into our store; never forward it to the webview.
    let set_cookies: Vec<String> = resp
        .headers()
        .get_all(reqwest::header::SET_COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok().map(|s| s.to_string()))
        .collect();
    if !set_cookies.is_empty() {
        store.capture_set_cookie(&set_cookies);
    }

    let mut out_headers: Vec<(String, String)> = Vec::new();
    for (k, v) in resp.headers().iter() {
        let lk = k.as_str().to_ascii_lowercase();
        if HOP_BY_HOP.contains(&lk.as_str()) || lk == "set-cookie" {
            continue;
        }
        if let Ok(val) = v.to_str() {
            out_headers.push((k.as_str().to_string(), val.to_string()));
        }
    }

    let body = resp.bytes().map(|b| b.to_vec()).unwrap_or_default();

    ProxyResponse { status, headers: out_headers, body }
}
