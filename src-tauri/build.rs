const COMMANDS: &[&str] = &["desktop_bootstrap", "retry_backend"];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build Lyra desktop shell metadata");
}
