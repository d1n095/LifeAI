# MainAI Cross-Platform App (Tauri 2) — Foundation

Installable MainAI for **macOS (Apple Silicon)** and **Android (Pixel)** that reuses the
existing `frontend/` UI and the existing `backend/` APIs + Security Kernel verbatim. The
browser web app is **not** changed — this is an additive, separate application shell.

> Status: **foundation / Phase 1–2 scaffold.** This directory is buildable source. See
> "Build status & honesty notes" for exactly what has and has not been compiled on this
> machine. Nothing here is self-certified — a separate authorized examiner reviews it.

---

## Why a separate shell (not SSR-in-Tauri)

`frontend/` is a Next.js 16 app-router application, but in practice it is a **client SPA**:

- Every page is a client component; there is no `getServerSideProps`, no `cookies()` /
  `headers()` usage, and no `'use server'` action (verified in the tree).
- The only server-side pieces are two route handlers: `app/api/[...path]/route.ts` (a
  same-origin proxy to the backend) and `app/api/edge-probe/route.ts`.

A Tauri bundle has **no Next.js server**, so SSR and those route handlers cannot run inside
it. We therefore ship a **static export** of the SPA as bundled assets and reproduce the one
server behavior that matters — the `/api/*` proxy — in the Tauri **Rust** layer. This is the
mapping:

```
WEB:    browser → Next route handler app/api/[...path] → backend   (HttpOnly cookie same-origin)
TAURI:  webview → Rust custom-scheme proxy (src-tauri/src/proxy.rs) → backend   (cookie jar in Rust)
```

## Security model (maps 1:1 to the assignment's security requirements)

| Requirement | How this shell satisfies it |
|---|---|
| Security Kernel stays authoritative on the backend | The shell calls the *same* backend `/api/*`. No auth/authorization logic is reimplemented client-side. |
| No privileged secrets embedded in packages | The package embeds only the backend **URL** (not a secret) and static UI. No API keys, no service credentials. |
| Platform-protected session storage | Session cookies (`access_token`, `refresh_token`, both HttpOnly) live **only** in the Rust cookie jar, persisted to the OS-protected per-user app-data dir; the refresh token is additionally sealable into the OS keychain/keystore (`keyring`). They are **never** handed to the webview/JS — the Rust proxy strips `Set-Cookie` from what the webview sees, exactly as the threat model intends. |
| Sensitive approvals require fresh authorization | Approval calls go through the backend's existing step-up flow; the shell adds no "unlocked device == authorized" shortcut. (Phase 2 wires the step-up re-auth prompt; see `proxy.rs` `STEP_UP_PATHS`.) |
| Strict owner isolation | Unchanged — enforced by backend RLS + the Security Kernel. The shell is a single-owner client. |
| No unauthorized file/camera/mic/notification access | Tauri **capabilities** (`src-tauri/capabilities/default.json`) grant a minimal allow-list only. No `shell`, no arbitrary `fs`, no `http` host access from JS. |
| No unrestricted WebView bridge | The webview can reach exactly one thing: the `mainaiapi:` proxy scheme. A strict CSP (`tauri.conf.json`) blocks all other network egress from the webview. The Rust↔JS command surface is a short, named allow-list — not a generic host bridge. |
| Backend agents keep running regardless of app state | Autonomous agent execution is server-side; the apps are **observers/controllers**. Closing the app or Android backgrounding it does not stop backend work. |

## Layout

```
desktop/
  README.md                     ← this file
  package.json                  ← orchestration (export frontend, run/build tauri)
  scripts/
    prepare-frontend-export.sh  ← non-invasive static export of ../frontend into .app-build/out
  src-tauri/
    Cargo.toml
    build.rs
    tauri.conf.json             ← app metadata, bundle, strict CSP, windows
    capabilities/default.json   ← minimal capability allow-list
    src/
      main.rs
      lib.rs                    ← app setup, registers the mainaiapi: proxy scheme
      proxy.rs                  ← the /api proxy: cookie jar, Set-Cookie capture, egress allow-list
    icons/                      ← generated with `cargo tauri icon` (see build instructions)
```

The static export is written to `desktop/.app-build/out` (git-ignored) and referenced by
`tauri.conf.json` as `frontendDist`. The real `../frontend` tree is **never mutated**; the
export runs against a prepared copy with the server-only `app/api` routes removed and
`output: "export"` set, so the public web deployment config is untouched.

## Build instructions (reproducible)

Prereqs: Node 20+ (have v26), Rust stable (`rustup`), and for each target:
- **macOS `.app`/`.dmg`**: Xcode Command Line Tools (have them). Full Xcode not required for an
  unsigned bundle.
- **Android `.apk`**: Android SDK + NDK + a JDK 17, and `rustup target add
  aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android`.

```bash
# from desktop/
npm install                       # installs @tauri-apps/cli
npm run export:frontend           # static-export ../frontend -> .app-build/out  (no Rust needed)

# macOS desktop:
npm run tauri -- build            # -> src-tauri/target/release/bundle/{macos,dmg}

# Android (after `tauri android init` and SDK/NDK/JDK present):
npm run tauri -- android init
npm run tauri -- android build --apk --debug
```

Dev backend defaults to `http://localhost:8000` (override with `MAINAI_BACKEND_URL` at build
time). Point it at the production HTTPS backend for a real installable build.

## Build status & honesty notes (this machine, 2026-10-29)

- `node`/`npm`: present (v26.9.0 / 11.19.1).
- `rust`/`cargo`: installed (rustc 1.99.1, host `aarch64-apple-darwin`).
- Full Xcode: **not** installed (only Command Line Tools) — fine for an unsigned `.app`/`.dmg`.
- Android SDK/NDK/JDK: **not installed** (multi-GB). A *verified* APK requires installing that
  toolchain; the source + icons here are complete and buildable, but no APK is claimed as built.

### macOS — VERIFIED BUILT ✅
- Static export of `../frontend` succeeds; all routes export except the deferred
  `library/[id]` detail (see §4b of the export script — follow-up: query-param page or SPA
  fallback). The library *list* and every other route are included.
- `src-tauri/target/release/bundle/dmg/MainAI_0.1.0_aarch64.dmg` built (≈5.0 MiB), containing
  `MainAI.app` + an `/Applications` symlink. Bundle identifier `ai.mainai.client`; binary is a
  native **arm64 Mach-O**. The Next.js SPA is embedded in the binary (31 `_next/static` asset
  refs) and the `mainaiapi:` proxy scheme is compiled in.
- The app **launches and runs without error** (process stays alive; no panic on startup →
  SessionStore load, scheme registration and window creation all succeed).
- NOT yet eyeballed: a screenshot of the rendered login UI — this machine's terminal lacks
  macOS Screen Recording permission, so the rendered window could not be captured from the
  build process. Open the DMG (or `open src-tauri/target/release/bundle/macos/MainAI.app` after
  a `--bundles app` build) to see it. With no backend on `http://localhost:8000`, the login
  screen renders and API calls return the proxy's 502 until a backend is running.

### Android — NOT BUILT (toolchain absent)
Source + mobile entry point (`#[cfg_attr(mobile, …)]`) and icon set are ready. Building a real
APK needs the Android SDK + NDK + JDK 17 (multi-GB) and `tauri android init`. No APK is claimed
as built until that toolchain is installed and the APK is actually produced and verified.

No DMG or APK is reported as "working" anywhere unless it was actually built and launched.
