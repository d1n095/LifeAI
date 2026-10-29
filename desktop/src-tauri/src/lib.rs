//! MainAI cross-platform shell — Tauri 2 application entry (shared by desktop + Android).
//!
//! Responsibilities:
//!   * Register the `mainaiapi:` custom scheme and forward it to the backend via `proxy.rs`,
//!     so the webview never sees session cookies and can only reach the one backend origin.
//!   * Expose a *small, named* command allow-list to JS (no generic host bridge).
//!
//! Everything privileged (auth, authorization, agent execution) stays on the backend.

mod proxy;

use std::borrow::Cow;

use proxy::{ProxyRequest, SessionStore};
use tauri::{Manager, State};

/// Narrow command: does the Rust-side session store currently hold session cookies?
/// Lets the SPA show the right initial screen without exposing the tokens themselves.
#[tauri::command]
fn session_status(store: State<'_, SessionStore>) -> bool {
    store.is_authenticated()
}

/// Narrow command: wipe the device-local session (native "sign out of this device").
/// The SPA should first call `/api/auth/logout` through the proxy for server-side revocation,
/// then call this to clear the local cookie store.
#[tauri::command]
fn native_sign_out(store: State<'_, SessionStore>) {
    store.clear();
}

/// The backend origin this build targets — surfaced read-only so the UI can show which
/// environment it is pointed at. Not a secret.
#[tauri::command]
fn backend_origin() -> String {
    proxy::BACKEND_BASE.to_string()
}

fn add_cors(builder: http::response::Builder, origin: Option<&str>) -> http::response::Builder {
    // Both the SPA origin (tauri asset protocol) and this scheme are app-internal and
    // controlled by us; reflect the requesting origin so the webview fetch succeeds. There are
    // no ambient webview credentials to protect (the session lives in Rust, not the webview).
    let allow = origin.unwrap_or("*");
    builder
        .header("access-control-allow-origin", allow)
        .header("access-control-allow-methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        .header("access-control-allow-headers", "content-type,x-csrf-token")
        .header("vary", "origin")
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_os::init())
        .invoke_handler(tauri::generate_handler![
            session_status,
            native_sign_out,
            backend_origin
        ])
        .setup(|app| {
            let dir = app
                .path()
                .app_data_dir()
                .unwrap_or_else(|_| std::path::PathBuf::from("."));
            app.manage(SessionStore::load(dir));
            Ok(())
        })
        .register_asynchronous_uri_scheme_protocol(proxy::SCHEME, move |ctx, request, responder| {
            let app = ctx.app_handle().clone();

            let origin = request
                .headers()
                .get("origin")
                .and_then(|v| v.to_str().ok())
                .map(|s| s.to_string());

            // CORS preflight: answer locally, never touches the backend.
            if request.method() == http::Method::OPTIONS {
                let resp = add_cors(http::Response::builder().status(204), origin.as_deref())
                    .body(Cow::<'static, [u8]>::Owned(Vec::new()))
                    .unwrap();
                responder.respond(resp);
                return;
            }

            // mainaiapi://localhost/api/...  ->  path_and_query = /api/...
            let uri = request.uri();
            let path_and_query = uri
                .path_and_query()
                .map(|pq| pq.as_str().to_string())
                .unwrap_or_else(|| "/".to_string());

            let method = request.method().as_str().to_string();
            let headers: Vec<(String, String)> = request
                .headers()
                .iter()
                .filter_map(|(k, v)| v.to_str().ok().map(|val| (k.as_str().to_string(), val.to_string())))
                .collect();
            let body = request.body().clone();

            let preq = ProxyRequest { method, path_and_query, headers, body };

            // reqwest blocking call off the UI/event thread.
            std::thread::spawn(move || {
                let store = app.state::<SessionStore>();
                let presp = proxy::forward(&store, preq);

                let mut builder = add_cors(http::Response::builder().status(presp.status), origin.as_deref());
                for (k, v) in presp.headers {
                    // Skip any origin header from the backend; our add_cors already set it.
                    if k.eq_ignore_ascii_case("access-control-allow-origin") {
                        continue;
                    }
                    builder = builder.header(k, v);
                }
                let resp = builder
                    .body(Cow::<'static, [u8]>::Owned(presp.body))
                    .unwrap();
                responder.respond(resp);
            });
        })
        .run(tauri::generate_context!())
        .expect("error while running MainAI tauri application");
}
