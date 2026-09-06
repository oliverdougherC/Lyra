const COMMANDS: &[&str] = &[
    "desktop_bootstrap",
    "desktop_print",
    "desktop_backup_create",
    "desktop_backup_restore",
    "desktop_update_status",
    "desktop_update_recovery",
    "check_desktop_update",
    "download_desktop_update",
    "cancel_desktop_update",
    "install_desktop_update",
    "restart_desktop_update",
    "retry_backend",
    "open_external_url",
    "pick_import_directory",
    "save_original_document",
    "pick_workspace_directory",
    "publish_desktop_import",
];

fn main() {
    println!("cargo:rerun-if-changed=tauri.conf.json");
    let config: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string("tauri.conf.json").expect("Tauri config"))
            .expect("valid Tauri config");
    println!(
        "cargo:rustc-env=LYRA_BUILD_NUMBER={}",
        config["bundle"]["macOS"]["bundleVersion"]
            .as_str()
            .expect("macOS build number")
    );
    println!("cargo:rerun-if-changed=../backend/storage/migrations");
    let schema = std::fs::read_dir("../backend/storage/migrations")
        .expect("migration directory")
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let name = entry.file_name();
            let name = name.to_str()?;
            if !name.ends_with(".sql") {
                return None;
            }
            name.split('_').next()?.parse::<u64>().ok()
        })
        .max()
        .expect("numbered migrations");
    println!("cargo:rustc-env=LYRA_SCHEMA_VERSION={schema}");
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build Lyra desktop shell metadata");
}
