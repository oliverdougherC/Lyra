//! Explicit updates only. The supported Tauri updater owns signature verification and
//! application replacement; student data and Keychain are never replacement targets.
use serde::Serialize;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_updater::{Update, UpdaterExt};
use tokio::sync::Notify;

const MAX_ARCHIVE_BYTES: u64 = 512 * 1024 * 1024;
const CHANNEL_ROOT: &str = "https://oliverdougherC.github.io/Lyra";

#[derive(Default)]
pub(crate) struct UpdateState {
    inner: Mutex<Session>,
}
#[derive(Default)]
struct Session {
    phase: &'static str,
    checked_at: Option<u64>,
    candidate: Option<Update>,
    bytes: Option<Vec<u8>>,
    downloaded: u64,
    unpacked: u64,
    total: Option<u64>,
    error: Option<String>,
    cancel: Option<Arc<Notify>>,
}
#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct UpdateStatus {
    current_version: String,
    current_build: String,
    recovery_available: bool,
    channel: &'static str,
    phase: &'static str,
    checked_at: Option<u64>,
    version: Option<String>,
    notes: Option<String>,
    downloaded: u64,
    total: Option<u64>,
    error: Option<String>,
}
fn channel(version: &str) -> &'static str {
    if version.contains('-') || version == "0.1.0" {
        "beta"
    } else {
        "stable"
    }
}
fn lock(state: &UpdateState) -> Result<std::sync::MutexGuard<'_, Session>, String> {
    state
        .inner
        .lock()
        .map_err(|_| "Update state is unavailable. Relaunch Lyra.".into())
}
fn busy(phase: &str) -> bool {
    matches!(
        phase,
        "checking" | "downloading" | "verifying" | "installing" | "restart"
    )
}
fn begin(state: &UpdateState, phase: &'static str) -> Result<(), String> {
    let mut session = lock(state)?;
    if busy(session.phase) {
        return Err("Another update operation is already active.".into());
    }
    session.phase = phase;
    session.error = None;
    Ok(())
}
fn fail(state: &UpdateState, error: String) -> String {
    if let Ok(mut session) = lock(state) {
        session.phase = "failed";
        session.error = Some(error.clone());
    }
    error
}
#[tauri::command]
pub(crate) fn desktop_update_status(
    app: AppHandle,
    state: State<'_, UpdateState>,
) -> Result<UpdateStatus, String> {
    let session = lock(&state)?;
    let current_version = app.package_info().version.to_string();
    Ok(UpdateStatus {
        current_build: env!("LYRA_BUILD_NUMBER").into(),
        recovery_available: crate::update_recovery::available(&app),
        channel: channel(&current_version),
        current_version,
        phase: if session.phase.is_empty() {
            "not-checked"
        } else {
            session.phase
        },
        checked_at: session.checked_at,
        version: session
            .candidate
            .as_ref()
            .map(|update| update.version.clone()),
        notes: session
            .candidate
            .as_ref()
            .and_then(|update| update.body.clone()),
        downloaded: session.downloaded,
        total: session.total,
        error: session.error.clone(),
    })
}
fn validate_manifest(raw: &serde_json::Value, url: &str, schema: u64) -> Result<u64, String> {
    let contract = &raw["lyra"];
    if contract["bundleIdentifier"] != "com.lyra.desktop" || contract["architecture"] != "aarch64" {
        return Err("This update is not a Lyra Apple Silicon application.".into());
    }
    let minimum = contract["schemaMin"]
        .as_u64()
        .ok_or("Update schema compatibility is missing.")?;
    let maximum = contract["schemaMax"]
        .as_u64()
        .ok_or("Update schema compatibility is missing.")?;
    if minimum > schema || maximum < schema {
        return Err(
            "This update cannot read your current data schema. Keep this app installed.".into(),
        );
    }
    // Publication is pinned to this repository. The Tauri client follows GitHub's
    // authenticated HTTPS CDN redirect; neither feed nor renderer supplies another host.
    if !url.starts_with("https://github.com/oliverdougherC/Lyra/releases/download/") {
        return Err("The update download is not from Lyra's release repository.".into());
    }
    let size = contract["size"]
        .as_u64()
        .ok_or("Update download size is missing.")?;
    if size == 0 || size > MAX_ARCHIVE_BYTES {
        return Err("The update exceeds the supported download size.".into());
    }
    Ok(size)
}
async fn current_schema(app: &AppHandle) -> Result<u64, String> {
    let bootstrap = {
        let state = app.state::<crate::AppState>();
        let lifecycle = state
            .lifecycle
            .lock()
            .map_err(|_| "Backend state is unavailable.")?;
        lifecycle
            .backend
            .as_ref()
            .ok_or("The backend must be ready before updating.")?
            .bootstrap
            .clone()
    };
    let response = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|_| "Could not prepare update compatibility check.")?
        .get(format!(
            "{}/api/health/update-schema",
            bootstrap.api_base.trim_end_matches('/')
        ))
        .header(bootstrap.session_header_name, bootstrap.session_secret)
        .send()
        .await
        .map_err(|_| "Could not read your current data schema. Retry when the backend is ready.")?
        .error_for_status()
        .map_err(|_| "Your current data schema could not be verified. Update stopped.")?;
    let schema: serde_json::Value = response
        .json()
        .await
        .map_err(|_| "Invalid data compatibility response.")?;
    schema["version"]
        .as_u64()
        .ok_or_else(|| "Data compatibility response has no schema version.".into())
}

#[tauri::command]
pub(crate) async fn check_desktop_update(
    app: AppHandle,
    state: State<'_, UpdateState>,
) -> Result<(), String> {
    begin(&state, "checking")?;
    {
        let mut session = lock(&state)?;
        session.candidate = None;
        session.bytes = None;
        session.downloaded = 0;
        session.total = None;
    }
    let result = async {
        if !cfg!(all(target_os = "macos", target_arch = "aarch64")) { return Err("Updates currently support Apple Silicon macOS only.".into()); }
        let endpoint = format!("{CHANNEL_ROOT}/{}/latest.json", channel(&app.package_info().version.to_string()));
        let updater = app.updater_builder().endpoints(vec![endpoint.parse().map_err(|_| "Invalid update endpoint.")?])
            .map_err(|_| "Invalid update endpoint.")?.timeout(Duration::from_secs(120)).build().map_err(|_| "The signed updater is not configured.")?;
        let candidate = updater.check().await.map_err(|_| "The update feed could not be checked. Your app and data are unchanged. Try again later.")?;
        let total = match &candidate { Some(update) => Some(validate_manifest(&update.raw_json, update.download_url.as_str(), current_schema(&app).await?)?), None => None };
        let mut session = lock(&state)?;
        session.phase = if candidate.is_some() { "available" } else { "up-to-date" };
        session.candidate = candidate; session.total = total;
        Ok(())
    }.await;
    lock(&state)?.checked_at = Some(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
    );
    result.map_err(|error| fail(&state, error))
}

#[cfg(unix)]
fn available_space(path: &std::path::Path) -> Result<u64, String> {
    use std::os::unix::ffi::OsStrExt;
    let name = std::ffi::CString::new(path.as_os_str().as_bytes())
        .map_err(|_| "Invalid application path.")?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: name is live and NUL terminated; statvfs initializes stats on success.
    if unsafe { libc::statvfs(name.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return Err("Could not check free disk space.".into());
    }
    let stats = unsafe { stats.assume_init() };
    Ok(u64::from(stats.f_bavail).saturating_mul(stats.f_frsize))
}
fn require_space(size: u64, available: u64) -> Result<(), String> {
    if available < size.saturating_mul(6).saturating_add(512 * 1024 * 1024) {
        return Err("There is not enough free disk space to safely stage and replace Lyra. Free space and try again.".into());
    }
    Ok(())
}
fn check_disk(size: u64) -> Result<(), String> {
    #[cfg(unix)]
    {
        require_space(size, available_space(&std::env::temp_dir())?)?;
        let executable = std::env::current_exe().map_err(|_| "Cannot locate the installed app.")?;
        require_space(
            size,
            available_space(
                executable
                    .parent()
                    .ok_or("Cannot locate the installed app.")?,
            )?,
        )?;
    }
    Ok(())
}
#[tauri::command]
pub(crate) async fn download_desktop_update(state: State<'_, UpdateState>) -> Result<(), String> {
    let (update, expected, cancel) = {
        let mut session = lock(&state)?;
        if busy(session.phase) {
            return Err("Another update operation is already active.".into());
        }
        let update = session
            .candidate
            .clone()
            .ok_or("Check for an update first.")?;
        let expected = session.total.ok_or("Update size is missing.")?;
        let cancel = Arc::new(Notify::new());
        session.cancel = Some(cancel.clone());
        session.phase = "downloading";
        session.downloaded = 0;
        session.bytes = None;
        session.error = None;
        (update, expected, cancel)
    };
    let result = async {
        check_disk(expected)?;
        let (limit_tx, mut limit_rx) = tokio::sync::mpsc::unbounded_channel::<()>();
        let download = update.download(|chunk, total| {
            if let Ok(mut session) = lock(&state) {
                session.downloaded = session.downloaded.saturating_add(chunk as u64);
                if session.downloaded > expected || total.is_some_and(|length| length != expected) { let _ = limit_tx.send(()); }
            }
        }, || {});
        let bytes = tokio::select! {
            biased;
            _ = cancel.notified() => return Err("Download cancelled. Your app and data are unchanged.".into()),
            _ = limit_rx.recv() => return Err("Download size did not match the release manifest.".into()),
            result = download => result.map_err(|_| "Download or signature verification failed. Your app and data are unchanged. Check again to retry.")?,
        };
        if bytes.len() as u64 != expected { return Err("Download size did not match the release manifest.".into()); }
        let version = update.version.clone();
        let feed = update.raw_json.clone();
        lock(&state)?.phase = "verifying";
        let (bytes, unpacked) = tauri::async_runtime::spawn_blocking(move || {
            let unpacked = crate::update_archive::validate(&bytes, &version, &feed)?;
            Ok::<_, String>((bytes, unpacked))
        }).await.map_err(|_| "Archive verification stopped unexpectedly.")??;
        let mut session = lock(&state)?; session.bytes = Some(bytes); session.unpacked = unpacked; session.phase = "ready";
        Ok(())
    }.await;
    result.map_err(|error| fail(&state, error))
}
#[tauri::command]
pub(crate) fn cancel_desktop_update(state: State<'_, UpdateState>) -> Result<(), String> {
    let session = lock(&state)?;
    if session.phase == "downloading" {
        if let Some(cancel) = &session.cancel {
            cancel.notify_one();
        }
    }
    Ok(())
}
#[tauri::command]
pub(crate) async fn install_desktop_update(
    app: AppHandle,
    state: State<'_, UpdateState>,
) -> Result<(), String> {
    let (update, bytes, unpacked) = {
        let mut session = lock(&state)?;
        if session.phase != "ready" {
            return Err("Download and verify the update first.".into());
        }
        let update = session
            .candidate
            .clone()
            .ok_or("Check for an update first.")?;
        let bytes = session.bytes.take().ok_or("Download the update first.")?;
        session.phase = "installing";
        (update, bytes, session.unpacked)
    };
    let result = async {
        validate_manifest(&update.raw_json, update.download_url.as_str(), current_schema(&app).await?)?;
        check_disk((bytes.len() as u64).max(unpacked))?;
        let backup_app = app.clone();
        tauri::async_runtime::spawn_blocking(move || crate::update_recovery::retain(&backup_app))
            .await.map_err(|_| "App backup stopped unexpectedly. Update stopped.")??;
        crate::stop_for_update(app.clone()).await?;
        let installed = tauri::async_runtime::spawn_blocking(move || update.install(bytes)).await;
        if !matches!(&installed, Ok(Ok(()))) {
            let resumed = crate::resume_after_failed_update(app.clone()).await;
            return Err(if resumed.is_ok() { "Application replacement failed. Your backend resumed and data is retained. Check again to retry." } else { "Application replacement failed and the backend could not resume. Use Show previous Lyra app, quit this window, and open the retained app in Finder; your student data is retained." }.into());
        }
        lock(&state)?.phase = "restart";
        Ok(())
    }.await;
    result.map_err(|error| fail(&state, error))
}
#[tauri::command]
pub(crate) fn restart_desktop_update(
    app: AppHandle,
    state: State<'_, UpdateState>,
) -> Result<(), String> {
    if lock(&state)?.phase != "restart" {
        return Err("No installed update is waiting to restart.".into());
    }
    app.restart();
}

#[cfg(test)]
mod tests {
    use super::*;
    fn manifest() -> serde_json::Value {
        serde_json::json!({"lyra":{"bundleIdentifier":"com.lyra.desktop","architecture":"aarch64","schemaMin":0,"schemaMax":44,"size":100}})
    }
    const URL: &str =
        "https://github.com/oliverdougherC/Lyra/releases/download/v0.2.0-beta.0/Lyra.app.tar.gz";
    #[test]
    fn packaged_plugin_configuration_is_deserializable_and_pinned() {
        let app: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let config: tauri_plugin_updater::Config =
            serde_json::from_value(app["plugins"]["updater"].clone()).unwrap();
        assert_eq!(
            config.pubkey.trim(),
            include_str!("../updater-public-key.txt").trim()
        );
        assert!(config.endpoints.is_empty());
        assert!(!config.dangerous_insecure_transport_protocol);
        assert!(!config.dangerous_accept_invalid_certs);
    }

    #[test]
    fn refuses_wrong_architecture_identity_schema_origin_and_size() {
        assert_eq!(validate_manifest(&manifest(), URL, 44).unwrap(), 100);
        for (key, value) in [
            ("architecture", serde_json::json!("x86_64")),
            ("bundleIdentifier", serde_json::json!("com.other.app")),
            ("schemaMax", serde_json::json!(43)),
            ("schemaMin", serde_json::json!(45)),
            ("size", serde_json::json!(MAX_ARCHIVE_BYTES + 1)),
        ] {
            let mut value_manifest = manifest();
            value_manifest["lyra"][key] = value;
            assert!(
                validate_manifest(&value_manifest, URL, 44).is_err(),
                "{key}"
            );
        }
        assert!(validate_manifest(&manifest(), "https://evil.invalid/update", 44).is_err());
    }
    #[test]
    fn disk_exhaustion_refuses_replacement() {
        assert!(require_space(100, 0).is_err());
        assert!(require_space(100, u64::MAX).is_ok());
    }
    #[test]
    fn channels_never_use_latest_release_prerelease_selection() {
        assert_eq!(channel("0.1.0"), "beta");
        assert_eq!(channel("0.2.0-beta.2"), "beta");
        assert_eq!(channel("0.2.0"), "stable");
    }
}
