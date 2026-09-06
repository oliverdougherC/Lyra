//! User-selected packaged backups share the native lifecycle owner.
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct BackupResult {
    status: String,
    label: String,
}

fn execute(app: AppHandle, operation: &str, path: PathBuf) -> Result<BackupResult, String> {
    let state = app.state::<crate::AppState>();
    let mut lifecycle = state
        .lifecycle
        .lock()
        .map_err(|_| "Lyra's lifecycle is unavailable")?;
    if state.quitting.load(std::sync::atomic::Ordering::SeqCst)
        || state.updating.load(std::sync::atomic::Ordering::SeqCst)
    {
        return Err("Finish the current update or quit operation first".into());
    }
    let sidecar = if let Some(existing) = lifecycle.backend.as_mut() {
        let sidecar = existing.sidecar_path.clone();
        crate::stop_backend(existing).map_err(|error| error.to_string())?;
        sidecar
    } else {
        crate::resolve_sidecar_path(&app).map_err(|error| error.to_string())?
    };
    lifecycle.backend = None;
    let mut command = Command::new(sidecar);
    command
        .arg(format!("--desktop-backup-{operation}"))
        .arg(path)
        .env("LYRA_PACKAGED", "1");
    let output = crate::bounded_process::run(&mut command, Duration::from_secs(300));
    // A failed helper may have crossed a journaled restore rename. Restart runs the
    // recovery protocol before opening SQLite; never claim preservation from exit status.
    let restarted = crate::launch_backend(&app).map_err(|error| error.to_string());
    match restarted {
        Ok(backend) => lifecycle.backend = Some(backend),
        Err(error) => return Err(format!("Backup recovery needs attention: {error}")),
    }
    let output = output.map_err(|_| "The backup operation timed out or could not start. Lyra reopened its data; retry with a smaller backup.".to_string())?;
    let result: BackupResult = serde_json::from_slice(&output.stdout)
        .map_err(|_| "The backup helper returned an invalid response".to_string())?;
    if !output.status.success() || result.status == "error" {
        return Err(result.label);
    }
    Ok(result)
}

#[tauri::command]
pub(crate) async fn desktop_backup_create(app: AppHandle) -> Result<BackupResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let selection = app
            .dialog()
            .file()
            .set_title("Save a Lyra backup")
            .set_file_name("Lyra-backup.tar.gz")
            .blocking_save_file();
        let Some(selection) = selection else {
            return Ok(BackupResult {
                status: "cancelled".into(),
                label: String::new(),
            });
        };
        let path = selection
            .into_path()
            .map_err(|_| "Choose a local backup destination")?;
        execute(app, "create", path)
    })
    .await
    .map_err(|_| "The backup worker stopped unexpectedly".to_string())?
}

#[tauri::command]
pub(crate) async fn desktop_backup_restore(app: AppHandle) -> Result<BackupResult, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let selection = app.dialog().file()
            .set_title("Restore a Lyra backup")
            .add_filter("Lyra backups", &["gz", "tgz", "tar"])
            .blocking_pick_file();
        let Some(selection) = selection else {
            return Ok(BackupResult { status: "cancelled".into(), label: String::new() });
        };
        let path = selection.into_path().map_err(|_| "Choose a local backup archive")?;
        let window = app.get_webview_window("main").ok_or("Lyra main window is unavailable")?;
        let confirmed = app.dialog()
            .message("Restore this backup and replace the classes, documents, and drafts currently in Lyra? Your current data folder will be retained as a recovery copy. Lyra will reopen when restoration finishes.")
            .title("Restore backup?")
            .parent(&window)
            .buttons(MessageDialogButtons::OkCancelCustom("Restore backup".into(), "Cancel".into()))
            .blocking_show();
        if !confirmed {
            return Ok(BackupResult { status: "cancelled".into(), label: String::new() });
        }
        execute(app, "restore", path)
    }).await.map_err(|_| "The restore worker stopped unexpectedly".to_string())?
}
