const COMMANDS: &[&str] = &[
    "desktop_bootstrap",
    "retry_backend",
    "open_external_url",
    "pick_import_directory",
    "pick_workspace_directory",
    "publish_desktop_import",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build Lyra desktop shell metadata");
}
