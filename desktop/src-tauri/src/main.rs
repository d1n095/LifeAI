// Prevents an extra console window on Windows in release. Harmless on macOS/Android.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mainai_app_lib::run();
}
