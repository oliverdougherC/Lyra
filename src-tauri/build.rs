const COMMANDS: &[&str] = &[
    "desktop_bootstrap",
    "native_chat_mount",
    "native_chat_set_frame",
    "native_chat_set_content_height",
    "native_chat_render",
    "native_chat_action",
    "native_chat_scroll_to_bottom",
    "native_chat_set_scroll_ratio",
    "native_chat_show",
    "native_chat_hide",
    "native_chat_unmount",
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
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        let out = std::env::var("OUT_DIR").expect("OUT_DIR");
        let object = format!("{out}/native_chat.o");
        let archive = format!("{out}/liblyra_native_chat.a");
        assert!(Command::new("clang")
            .args(["-fobjc-arc", "-c", "src/native_chat.m", "-o", &object])
            .status()
            .expect("clang")
            .success());
        assert!(Command::new("ar")
            .args(["crs", &archive, &object])
            .status()
            .expect("ar")
            .success());
        println!("cargo:rustc-link-search=native={out}");
        println!("cargo:rustc-link-lib=static=lyra_native_chat");
        println!("cargo:rustc-link-lib=framework=AppKit");
        println!("cargo:rustc-link-lib=framework=WebKit");
        println!("cargo:rerun-if-changed=src/native_chat.m");
    }
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
