mod backup;
mod bounded_process;
mod external_navigation;
mod update_archive;
mod update_recovery;
mod updater;

use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::DialogExt;

const PROTOCOL_VERSION: u8 = 1;
const SESSION_HEADER_NAME: &str = "X-Lyra-Session";
const LOOPBACK_CLIENT_HEADER_NAME: &str = "X-Lyra-Client";
const SIDECAR_NAME: &str = "lyra-backend";
const MAX_READY_LINE_BYTES: usize = 512;
const MAX_HTTP_STATUS_LINE_BYTES: usize = 1024;
const READY_TIMEOUT: Duration = Duration::from_secs(20);
const SHUTDOWN_GRACE_TIMEOUT: Duration = Duration::from_secs(8);
const TERMINAL_FAILURE_DIAGNOSTIC_WAIT: Duration = Duration::from_millis(400);
const MAX_STDERR_TAIL_BYTES: usize = 2048;
const MAX_DIAGNOSTIC_CHARS: usize = 240;
const STARTUP_LOG_NAME: &str = "desktop-startup.log";
const STARTUP_LOG_ROTATE_BYTES: u64 = 65_536;
const STARTUP_LOG_BACKUPS: usize = 3;
const MAX_LOG_EVENT_CHARS: usize = 400;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct BootstrapPayload {
    protocol_version: u8,
    api_base: String,
    session_header_name: &'static str,
    session_secret: String,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct ImportSelectionPayload {
    selection_token: String,
    label: String,
}

#[derive(Debug, Serialize)]
struct ImportSelectionRecord<'a> {
    path: &'a str,
    label: &'a str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct CommandError {
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    code: Option<&'static str>,
}

impl From<LaunchError> for CommandError {
    fn from(value: LaunchError) -> Self {
        Self {
            message: value.to_string(),
            code: None,
        }
    }
}

impl From<external_navigation::ExternalNavigationError> for CommandError {
    fn from(value: external_navigation::ExternalNavigationError) -> Self {
        Self {
            message: value.to_string(),
            code: Some(value.code()),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
struct SidecarBootstrapRequest {
    protocol_version: u8,
    socket_fd: i32,
    parent_pid: u32,
    listener_addr: String,
    session_header_name: &'static str,
    session_secret: String,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct SidecarReady {
    status: String,
    protocol_version: u8,
    api_base: String,
    listener_addr: String,
    address_family: String,
    inherited_socket: bool,
    session_header_name: String,
    session_secret: String,
}

#[derive(Clone, Default)]
struct AppState {
    lifecycle: Arc<Mutex<Lifecycle>>,
    quitting: Arc<std::sync::atomic::AtomicBool>,
    updating: Arc<std::sync::atomic::AtomicBool>,
    shutdown_complete: Arc<std::sync::atomic::AtomicBool>,
}

#[derive(Default)]
struct Lifecycle {
    backend: Option<ManagedBackend>,
}

struct ManagedBackend {
    child: Child,
    sidecar_path: PathBuf,
    bootstrap: BootstrapPayload,
}

#[derive(Debug)]
enum LaunchError {
    Io {
        source: std::io::Error,
        diagnostics: Option<String>,
    },
    Json {
        source: serde_json::Error,
        diagnostics: Option<String>,
    },
    MissingSidecar {
        expected: Vec<String>,
    },
    ReadinessTimeout {
        diagnostics: Option<String>,
    },
    InvalidReadiness {
        reason: String,
        diagnostics: Option<String>,
    },
    HelperReclaim {
        diagnostics: Option<String>,
    },
    Import(&'static str),
    Poisoned,
}

impl fmt::Display for LaunchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io {
                source,
                diagnostics,
            } => write_diagnostic_suffix(
                f,
                &format!("desktop shell I/O failed: {source}"),
                diagnostics.as_deref(),
            ),
            Self::Json {
                source,
                diagnostics,
            } => write_diagnostic_suffix(
                f,
                &format!("desktop shell JSON failed: {source}"),
                diagnostics.as_deref(),
            ),
            Self::MissingSidecar { expected } => write!(
                f,
                "lyra-backend sidecar is not staged yet; expected one of {}",
                expected.join(", ")
            ),
            Self::ReadinessTimeout { diagnostics } => write_diagnostic_suffix(
                f,
                "lyra-backend did not report readiness within 20 seconds",
                diagnostics.as_deref(),
            ),
            Self::InvalidReadiness {
                reason,
                diagnostics,
            } => write_diagnostic_suffix(
                f,
                &format!("lyra-backend reported invalid readiness: {reason}"),
                diagnostics.as_deref(),
            ),
            Self::HelperReclaim { diagnostics } => write_diagnostic_suffix(
                f,
                "owned helper reclamation invariant failed",
                diagnostics.as_deref(),
            ),
            Self::Import(message) => write!(f, "desktop import failed: {message}"),
            Self::Poisoned => write!(f, "desktop shell state is unavailable after a prior panic"),
        }
    }
}

impl std::error::Error for LaunchError {}

impl LaunchError {
    fn invalid_readiness(reason: impl Into<String>) -> Self {
        Self::InvalidReadiness {
            reason: reason.into(),
            diagnostics: None,
        }
    }

    fn with_diagnostics(self, diagnostics: Option<String>) -> Self {
        match self {
            Self::Io { source, .. } => Self::Io {
                source,
                diagnostics,
            },
            Self::Json { source, .. } => Self::Json {
                source,
                diagnostics,
            },
            Self::ReadinessTimeout { .. } => Self::ReadinessTimeout { diagnostics },
            Self::InvalidReadiness { reason, .. } => Self::InvalidReadiness {
                reason,
                diagnostics,
            },
            other => other,
        }
    }
}

impl From<std::io::Error> for LaunchError {
    fn from(value: std::io::Error) -> Self {
        Self::Io {
            source: value,
            diagnostics: None,
        }
    }
}

impl From<serde_json::Error> for LaunchError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json {
            source: value,
            diagnostics: None,
        }
    }
}

fn write_diagnostic_suffix(
    f: &mut fmt::Formatter<'_>,
    prefix: &str,
    diagnostics: Option<&str>,
) -> fmt::Result {
    if let Some(detail) = diagnostics.filter(|detail| !detail.is_empty()) {
        write!(f, "{prefix}. stderr tail: {detail}")
    } else {
        write!(f, "{prefix}")
    }
}

impl AppState {
    fn ensure_backend(
        &self,
        app: &AppHandle,
        force_restart: bool,
    ) -> Result<BootstrapPayload, LaunchError> {
        let mut lifecycle = self.lifecycle.lock().map_err(|_| LaunchError::Poisoned)?;
        if self.quitting.load(std::sync::atomic::Ordering::SeqCst)
            || self.updating.load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(LaunchError::Import("Lyra is shutting down"));
        }

        if !force_restart {
            if let Some(existing) = lifecycle.backend.as_mut() {
                if child_is_running(&mut existing.child)? && backend_is_ready(&existing.bootstrap) {
                    return Ok(existing.bootstrap.clone());
                }
                log_event("backend health probe failed or process exited; preparing a restart");
                stop_backend(existing)?;
                lifecycle.backend = None;
            }
        }

        if force_restart {
            if let Some(existing) = lifecycle.backend.as_mut() {
                log_event("retry_backend requested; recycling owned backend");
                stop_backend(existing)?;
            }
            lifecycle.backend = None;
        }

        let managed = launch_backend(app)?;
        let bootstrap = managed.bootstrap.clone();
        lifecycle.backend = Some(managed);
        Ok(bootstrap)
    }

    fn shutdown(&self) -> Result<(), LaunchError> {
        let mut lifecycle = self.lifecycle.lock().map_err(|_| LaunchError::Poisoned)?;
        if let Some(existing) = lifecycle.backend.as_mut() {
            log_event("desktop shell is stopping its owned backend");
            stop_backend(existing)?;
        }
        lifecycle.backend = None;
        Ok(())
    }

    fn replace_application(
        &self,
        install: impl FnOnce() -> Result<(), String>,
    ) -> Result<(), String> {
        // The supported installer moves the app bundle in several steps. Share the
        // shutdown lock so a Quit worker cannot authorize exit between those moves.
        // This method is called only inside spawn_blocking, never on the UI thread.
        let lifecycle = self
            .lifecycle
            .lock()
            .map_err(|_| "Application lifecycle is unavailable.")?;
        if self.quitting.load(std::sync::atomic::Ordering::SeqCst) {
            return Err("Lyra is quitting; the update was not installed.".into());
        }
        if !self.updating.load(std::sync::atomic::Ordering::SeqCst) || lifecycle.backend.is_some() {
            return Err("The backend must be stopped for application replacement.".into());
        }
        install()
    }

    fn publish_import(&self, app: &AppHandle) -> Result<BootstrapPayload, LaunchError> {
        let mut lifecycle = self.lifecycle.lock().map_err(|_| LaunchError::Poisoned)?;
        if self.quitting.load(std::sync::atomic::Ordering::SeqCst)
            || self.updating.load(std::sync::atomic::Ordering::SeqCst)
        {
            return Err(LaunchError::Import("Lyra is shutting down"));
        }
        let sidecar_path = if let Some(existing) = lifecycle.backend.as_mut() {
            let path = existing.sidecar_path.clone();
            log_event("desktop import publication is stopping the owned backend");
            stop_backend(existing)?;
            path
        } else {
            resolve_sidecar_path(app)?
        };
        lifecycle.backend = None;

        let publication = run_import_publication(&sidecar_path);
        let managed = launch_backend(app)?;
        let bootstrap = managed.bootstrap.clone();
        lifecycle.backend = Some(managed);
        publication?;
        Ok(bootstrap)
    }
}

#[tauri::command]
async fn desktop_bootstrap(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.ensure_backend(&app, false))
        .await
        .map_err(|_| CommandError::from(LaunchError::Poisoned))?
        .map_err(|error| {
            log_event(&format!("backend bootstrap failed: {error}"));
            error.into()
        })
}

#[tauri::command]
async fn retry_backend(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.ensure_backend(&app, true))
        .await
        .map_err(|_| CommandError::from(LaunchError::Poisoned))?
        .map_err(|error| {
            log_event(&format!("backend retry failed: {error}"));
            error.into()
        })
}

#[tauri::command]
fn open_external_url(app: AppHandle, url: String) -> Result<(), CommandError> {
    external_navigation::open_external_url(&app, &url).map_err(|error| {
        log_event(&format!("external link rejected or failed: {error}"));
        error.into()
    })
}

/// The destination is chosen by the OS dialog and never returned to JavaScript.
#[tauri::command]
async fn save_original_document(
    app: AppHandle,
    filename: String,
    bytes: Vec<u8>,
) -> Result<bool, String> {
    if bytes.len() > 50 * 1024 * 1024 {
        return Err("The original document exceeds the upload limit.".into());
    }
    let filename: String = filename
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '_'
            }
        })
        .collect();
    let filename = filename.trim_start_matches('.');
    let selection = app
        .dialog()
        .file()
        .set_title("Save original document")
        .set_file_name(if filename.is_empty() {
            "document"
        } else {
            filename
        })
        .blocking_save_file();
    let Some(selection) = selection else {
        return Ok(false);
    };
    let path = selection
        .into_path()
        .map_err(|_| "The selected destination is not a local file.".to_owned())?;
    write_original_copy(&path, &bytes).map_err(|error| error.user_message().to_owned())?;
    Ok(true)
}

#[derive(Debug)]
enum OriginalCopyError {
    BeforePublication(std::io::Error),
    DestinationExists,
    Published(std::io::Error),
}

impl OriginalCopyError {
    fn user_message(&self) -> &'static str {
        match self {
            Self::DestinationExists => "That destination already exists. Choose another filename to save the original document.",
            Self::Published(_) => "The original document was saved, but final cleanup or durability could not be confirmed. Check the saved file before retrying.",
            Self::BeforePublication(_) => "The original document could not be saved to that destination.",
        }
    }
}

impl From<std::io::Error> for OriginalCopyError {
    fn from(error: std::io::Error) -> Self {
        Self::BeforePublication(error)
    }
}

impl fmt::Display for OriginalCopyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BeforePublication(error) => write!(
                formatter,
                "The original document could not be saved to that destination: {error}"
            ),
            Self::DestinationExists => formatter.write_str(
                "That destination already exists. Choose another filename to save the original document.",
            ),
            Self::Published(error) => write!(
                formatter,
                "The original document was saved, but final cleanup or durability confirmation failed: {error}. Check the saved file before retrying."
            ),
        }
    }
}

// The directory handle anchors creation, publication and cleanup to the same directory,
// even if a parent path is renamed. linkat publishes atomically without ever replacing
// an existing entry (including dangling symlinks and entries created during the write).
#[cfg(unix)]
fn write_original_copy(path: &Path, bytes: &[u8]) -> Result<(), OriginalCopyError> {
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::OpenOptionsExt;

    struct TemporaryCopy {
        directory: File,
        name: CString,
        owned: bool,
    }
    impl TemporaryCopy {
        fn remove(&mut self) -> std::io::Result<()> {
            if self.owned {
                // SAFETY: the owned directory fd and NUL-terminated name remain live.
                if unsafe { libc::unlinkat(self.directory.as_raw_fd(), self.name.as_ptr(), 0) } != 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                self.owned = false;
            }
            Ok(())
        }
    }
    impl Drop for TemporaryCopy {
        fn drop(&mut self) {
            let _ = self.remove();
        }
    }

    let invalid_path = || std::io::Error::other("The destination must name a local file");
    let destination = CString::new(path.file_name().ok_or_else(invalid_path)?.as_bytes())
        .map_err(|_| invalid_path())?;
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_CLOEXEC)
        .open(parent)?;
    // Do not open the destination: even special files must remain untouched.
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    // SAFETY: valid fd/name and writable stat storage; metadata is never read on error.
    let exists = unsafe {
        libc::fstatat(
            directory.as_raw_fd(),
            destination.as_ptr(),
            metadata.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    };
    if exists == 0 {
        return Err(OriginalCopyError::DestinationExists);
    }
    let error = std::io::Error::last_os_error();
    if error.kind() != std::io::ErrorKind::NotFound {
        return Err(error.into());
    }

    let name = CString::new(format!(
        ".lyra-original-{}.tmp",
        random_secret_hex().map_err(|error| std::io::Error::other(error.to_string()))?
    ))
    .unwrap();
    // SAFETY: valid directory fd and NUL-terminated sibling name. O_EXCL establishes
    // ownership before a cleanup guard is created; a collision is never removed.
    let fd = unsafe {
        libc::openat(
            directory.as_raw_fd(),
            name.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut temporary = TemporaryCopy {
        directory,
        name,
        owned: true,
    };
    // SAFETY: openat returned a new fd whose ownership transfers exactly once.
    let mut file = unsafe { File::from_raw_fd(fd) };
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::BeforeWrite, path)?;
    // A bounded first write also provides a deterministic partial-write fault point.
    let first_chunk = bytes.len().min(4096);
    file.write_all(&bytes[..first_chunk])?;
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::DuringWrite, path)?;
    file.write_all(&bytes[first_chunk..])?;
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::Flush, path)?;
    file.sync_all()?;
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::Publication, path)?;
    // SAFETY: both names are relative to the same live directory fd. Flags=0 and
    // linkat's no-replace semantics preserve any destination that appeared meanwhile.
    let published = unsafe {
        libc::linkat(
            temporary.directory.as_raw_fd(),
            temporary.name.as_ptr(),
            temporary.directory.as_raw_fd(),
            destination.as_ptr(),
            0,
        )
    };
    if published != 0 {
        let error = std::io::Error::last_os_error();
        return Err(if error.kind() == std::io::ErrorKind::AlreadyExists {
            OriginalCopyError::DestinationExists
        } else {
            error.into()
        });
    }
    // Publication has happened. Neither cleanup nor directory sync can roll it back;
    // all subsequent errors must explicitly tell the caller that the file was saved.
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::Cleanup, path)
        .map_err(OriginalCopyError::Published)?;
    temporary.remove().map_err(OriginalCopyError::Published)?;
    #[cfg(test)]
    original_copy_checkpoint(OriginalCopyStage::Durability, path)
        .map_err(OriginalCopyError::Published)?;
    temporary
        .directory
        .sync_all()
        .map_err(OriginalCopyError::Published)
}

#[cfg(not(unix))]
fn write_original_copy(_path: &Path, _bytes: &[u8]) -> Result<(), OriginalCopyError> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "Atomic original-document saving is not supported on this platform",
    )
    .into())
}

#[cfg(all(test, unix))]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OriginalCopyStage {
    BeforeWrite,
    DuringWrite,
    Flush,
    Publication,
    Cleanup,
    Durability,
}

#[cfg(all(test, unix))]
type OriginalCopyHook = Box<dyn FnMut(OriginalCopyStage, &Path) -> std::io::Result<()>>;
#[cfg(all(test, unix))]
thread_local! {
    static ORIGINAL_COPY_HOOK: std::cell::RefCell<Option<OriginalCopyHook>> = const { std::cell::RefCell::new(None) };
}
#[cfg(all(test, unix))]
fn original_copy_checkpoint(stage: OriginalCopyStage, path: &Path) -> std::io::Result<()> {
    ORIGINAL_COPY_HOOK.with(|hook| match hook.borrow_mut().as_mut() {
        Some(hook) => hook(stage, path),
        None => Ok(()),
    })
}

#[tauri::command]
async fn pick_import_directory(
    app: AppHandle,
) -> Result<Option<ImportSelectionPayload>, CommandError> {
    let selection = app
        .dialog()
        .file()
        .set_title("Choose an existing Lyra folder")
        .blocking_pick_folder();
    let Some(selection) = selection else {
        return Ok(None);
    };
    let path = selection
        .into_path()
        .map_err(|_| LaunchError::Import("the selected folder was not a local directory"))?;
    record_import_selection(&path).map(Some).map_err(Into::into)
}

#[tauri::command]
async fn pick_workspace_directory(app: AppHandle) -> Result<Option<String>, CommandError> {
    // The contextual agent's just-in-time attach: a normal native folder selection, no
    // path pasting. The path only ever reaches the backend's bounded-attach endpoint,
    // which re-validates the root (device/inode) server-side before anything is read.
    let selection = app
        .dialog()
        .file()
        .set_title("Choose a folder Lyra can work with")
        .blocking_pick_folder();
    let Some(selection) = selection else {
        return Ok(None);
    };
    let path = selection
        .into_path()
        .map_err(|_| LaunchError::Import("the selected folder was not a local directory"))?;
    Ok(Some(path.to_string_lossy().into_owned()))
}

#[tauri::command]
async fn publish_desktop_import(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || state.publish_import(&app))
        .await
        .map_err(|_| CommandError::from(LaunchError::Poisoned))?
        .map_err(|error| {
            log_event(&format!("desktop import publication failed: {error}"));
            error.into()
        })
}

pub(crate) async fn stop_for_update(app: AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>().inner().clone();
    state
        .updating
        .store(true, std::sync::atomic::Ordering::SeqCst);
    let restore = state.clone();
    let result = tauri::async_runtime::spawn_blocking(move || state.shutdown())
        .await
        .map_err(|_| "The update cleanup worker stopped unexpectedly".to_string())
        .and_then(|result| result.map_err(|error| error.to_string()));
    if result.is_err() {
        restore
            .updating
            .store(false, std::sync::atomic::Ordering::SeqCst);
    }
    result
}

pub(crate) async fn resume_after_failed_update(app: AppHandle) -> Result<(), String> {
    let state = app.state::<AppState>().inner().clone();
    state
        .updating
        .store(false, std::sync::atomic::Ordering::SeqCst);
    tauri::async_runtime::spawn_blocking(move || state.ensure_backend(&app, false))
        .await
        .map_err(|_| "The update recovery worker stopped unexpectedly".to_string())?
        .map(|_| ())
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn desktop_print(app: AppHandle) -> Result<(), String> {
    app.get_webview_window("main")
        .ok_or_else(|| "The document window is unavailable".to_string())?
        .print()
        .map_err(|_| "The macOS print dialog could not be opened".to_string())
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri_plugin_updater::Builder::new()
                .pubkey(include_str!("../updater-public-key.txt").trim())
                .build(),
        )
        .manage(updater::UpdateState::default())
        .plugin(
            tauri_plugin_opener::Builder::new()
                .open_js_links_on_click(false)
                .build(),
        )
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main_window(app);
        }))
        .setup(|app| {
            external_navigation::create_main_window(app)?;
            Ok(())
        })
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            desktop_bootstrap,
            desktop_print,
            retry_backend,
            open_external_url,
            pick_import_directory,
            save_original_document,
            pick_workspace_directory,
            publish_desktop_import,
            backup::desktop_backup_create,
            backup::desktop_backup_restore,
            updater::desktop_update_status,
            update_recovery::desktop_update_recovery,
            updater::check_desktop_update,
            updater::download_desktop_update,
            updater::cancel_desktop_update,
            updater::install_desktop_update,
            updater::restart_desktop_update
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Lyra desktop shell");

    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
            let state = app.state::<AppState>().inner().clone();
            let app = app.clone();
            let failed_app = app.clone();
            handle_exit_request(
                state,
                code,
                || api.prevent_exit(),
                move |code| app.exit(code),
                move || {
                    failed_app.dialog().message(
                        "Lyra could not confirm that its backend and helpers stopped. The app stayed open. Try Quit again; if the problem persists, contact support with the startup log."
                    ).title("Lyra could not quit safely")
                        .kind(tauri_plugin_dialog::MessageDialogKind::Error).show(|_| {});
                },
            );
        }
    });
}

// This is the signal/scheduling boundary registered with Tauri above. Tests use
// the same handler with observable exit callbacks; they do not force process exit.
fn handle_exit_request(
    state: AppState,
    code: Option<i32>,
    prevent_exit: impl FnOnce(),
    exit: impl FnOnce(i32) + Send + 'static,
    cleanup_failed: impl FnOnce() + Send + 'static,
) {
    if state
        .shutdown_complete
        .load(std::sync::atomic::Ordering::SeqCst)
    {
        return;
    }
    if state
        .quitting
        .swap(true, std::sync::atomic::Ordering::SeqCst)
    {
        prevent_exit();
        return;
    }
    prevent_exit();
    tauri::async_runtime::spawn_blocking(move || {
        if let Err(error) = state.shutdown() {
            log_event(&format!("terminal backend cleanup failed: {error}"));
            // A failed stop is not completion. Keep the shell alive and let another
            // explicit Quit retry the same owned backend instead of stranding it.
            state
                .quitting
                .store(false, std::sync::atomic::Ordering::SeqCst);
            cleanup_failed();
            return;
        }
        state
            .shutdown_complete
            .store(true, std::sync::atomic::Ordering::SeqCst);
        exit(code.unwrap_or(0));
    });
}

#[cfg(test)]
mod exit_request_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn ordinary_quit_allows_its_cleanup_triggered_final_exit() {
        let state = AppState::default();
        let prevented = AtomicUsize::new(0);
        let (final_tx, final_rx) = mpsc::channel();
        handle_exit_request(
            state.clone(),
            None,
            || {
                prevented.fetch_add(1, Ordering::SeqCst);
            },
            move |code| {
                final_tx.send(code).unwrap();
            },
            || panic!("unexpected cleanup failure"),
        );
        let code = final_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(code, 0);
        assert_eq!(prevented.load(Ordering::SeqCst), 1);
        handle_exit_request(
            state,
            Some(code),
            || {
                prevented.fetch_add(1, Ordering::SeqCst);
            },
            |_| panic!("cleanup must not run twice"),
            || panic!("unexpected cleanup failure"),
        );
        assert_eq!(
            prevented.load(Ordering::SeqCst),
            1,
            "completed cleanup must admit the final ordinary exit"
        );
    }

    #[test]
    fn repeated_quit_during_cleanup_waits_without_spawning_another_worker() {
        let state = AppState::default();
        let held = state.lifecycle.lock().unwrap();
        let (final_tx, final_rx) = mpsc::channel();
        let prevented = AtomicUsize::new(0);
        handle_exit_request(
            state.clone(),
            Some(7),
            || {
                prevented.fetch_add(1, Ordering::SeqCst);
            },
            move |code| {
                final_tx.send(code).unwrap();
            },
            || panic!("unexpected cleanup failure"),
        );
        handle_exit_request(
            state.clone(),
            Some(9),
            || {
                prevented.fetch_add(1, Ordering::SeqCst);
            },
            |_| panic!("duplicate quit scheduled cleanup"),
            || panic!("unexpected cleanup failure"),
        );
        assert_eq!(prevented.load(Ordering::SeqCst), 2);
        assert!(!state.shutdown_complete.load(Ordering::SeqCst));
        assert!(final_rx.try_recv().is_err());
        drop(held);
        assert_eq!(final_rx.recv_timeout(Duration::from_secs(2)).unwrap(), 7);
    }

    #[test]
    fn quit_cannot_complete_while_application_replacement_is_mutating() {
        let state = AppState::default();
        state.updating.store(true, Ordering::SeqCst);
        let installer_state = state.clone();
        let (entered_tx, entered_rx) = mpsc::channel();
        let (release_tx, release_rx) = mpsc::channel();
        let installer = std::thread::spawn(move || {
            installer_state.replace_application(|| {
                entered_tx.send(()).unwrap();
                release_rx.recv_timeout(Duration::from_secs(2)).unwrap();
                Ok(())
            })
        });
        entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        let (exit_tx, exit_rx) = mpsc::channel();
        handle_exit_request(
            state.clone(),
            None,
            || {},
            move |code| {
                exit_tx.send(code).unwrap();
            },
            || panic!("cleanup failed"),
        );
        let premature_exit = exit_rx.recv_timeout(Duration::from_millis(100));
        release_tx.send(()).unwrap();
        installer.join().unwrap().unwrap();
        assert!(
            premature_exit.is_err(),
            "Quit must not exit between the installer's app renames"
        );
        assert_eq!(exit_rx.recv_timeout(Duration::from_secs(2)).unwrap(), 0);
    }

    #[test]
    fn application_replacement_cannot_start_after_quit_latches() {
        let state = AppState::default();
        state.updating.store(true, Ordering::SeqCst);
        state.quitting.store(true, Ordering::SeqCst);
        let invoked = std::sync::atomic::AtomicBool::new(false);
        let result = state.replace_application(|| {
            invoked.store(true, Ordering::SeqCst);
            Ok(())
        });
        assert!(result.is_err());
        assert!(
            !invoked.load(Ordering::SeqCst),
            "a queued install must not mutate after Quit"
        );
    }

    #[test]
    fn cleanup_failure_keeps_the_shell_open_and_allows_an_explicit_retry() {
        let state = AppState::default();
        let _ = std::panic::catch_unwind(|| {
            let _held = state.lifecycle.lock().unwrap();
            panic!("injected prior lifecycle failure");
        });
        let (failed_tx, failed_rx) = mpsc::channel();
        let (exit_tx, exit_rx) = mpsc::channel();
        handle_exit_request(
            state.clone(),
            None,
            || {},
            move |code| {
                exit_tx.send(code).unwrap();
            },
            move || {
                failed_tx.send(()).unwrap();
            },
        );
        failed_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(!state.shutdown_complete.load(Ordering::SeqCst));
        assert!(!state.quitting.load(Ordering::SeqCst));
        assert!(
            exit_rx.try_recv().is_err(),
            "failed cleanup must not request final exit"
        );
        // Repair the injected condition and retry the same production handler.
        state.lifecycle.clear_poison();
        let (retry_tx, retry_rx) = mpsc::channel();
        handle_exit_request(
            state.clone(),
            None,
            || {},
            move |code| {
                retry_tx.send(code).unwrap();
            },
            || panic!("retry failed"),
        );
        assert_eq!(retry_rx.recv_timeout(Duration::from_secs(2)).unwrap(), 0);
        assert!(state.shutdown_complete.load(Ordering::SeqCst));
    }
}

fn launch_backend(app: &AppHandle) -> Result<ManagedBackend, LaunchError> {
    let sidecar = resolve_sidecar_path(app)?;
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let listener_addr = listener.local_addr()?.to_string();
    let session_secret = random_secret_hex()?;

    #[cfg(unix)]
    let listener_fd = listener.as_raw_fd();
    #[cfg(not(unix))]
    let listener_fd = -1;

    let bootstrap = SidecarBootstrapRequest {
        protocol_version: PROTOCOL_VERSION,
        socket_fd: listener_fd,
        parent_pid: std::process::id(),
        listener_addr,
        session_header_name: SESSION_HEADER_NAME,
        session_secret: session_secret.clone(),
    };

    let mut command = Command::new(&sidecar);
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    let _listener_inheritance = InheritedFdGuard::for_listener(&listener)?;

    let mut child = command.spawn()?;
    let ready = match bootstrap_child(&mut child, &bootstrap) {
        Ok(ready) => ready,
        Err(error) => {
            if let Err(cleanup_error) = stop_child(&mut child, None, Some(&sidecar)) {
                log_event(&format!(
                    "startup cleanup failed after launch error: {cleanup_error}"
                ));
                return Err(cleanup_error);
            }
            return Err(error);
        }
    };

    log_event("backend reported readiness");
    Ok(ManagedBackend {
        child,
        sidecar_path: sidecar,
        bootstrap: BootstrapPayload {
            protocol_version: PROTOCOL_VERSION,
            api_base: ready.api_base,
            session_header_name: SESSION_HEADER_NAME,
            session_secret,
        },
    })
}

fn bootstrap_child(
    child: &mut Child,
    expected: &SidecarBootstrapRequest,
) -> Result<SidecarReady, LaunchError> {
    let stderr = child.stderr.take().ok_or(LaunchError::InvalidReadiness {
        reason: "stderr pipe was not available".to_string(),
        diagnostics: None,
    })?;
    let diagnostics = StartupDiagnostics::spawn(stderr, expected.session_secret.clone());
    let result = (|| {
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| LaunchError::invalid_readiness("stdout pipe was not available"))?;
        write_bootstrap_request(child, expected)?;
        await_readiness(stdout, &diagnostics, expected)
    })();
    result.map_err(|error| diagnostics.finalize_failure(child, error))
}

fn write_bootstrap_request(
    child: &mut Child,
    bootstrap: &SidecarBootstrapRequest,
) -> Result<(), LaunchError> {
    let mut stdin = child.stdin.take().ok_or(LaunchError::invalid_readiness(
        "stdin pipe was not available",
    ))?;
    serde_json::to_writer(&mut stdin, bootstrap)?;
    stdin.write_all(b"\n")?;
    stdin.flush()?;
    drop(stdin);
    Ok(())
}

fn await_readiness(
    stdout: ChildStdout,
    diagnostics: &StartupDiagnostics,
    expected: &SidecarBootstrapRequest,
) -> Result<SidecarReady, LaunchError> {
    let (tx, rx) = mpsc::channel();
    let expected_for_thread = expected.clone();

    std::thread::spawn(move || {
        let outcome = read_readiness(stdout)
            .and_then(|ready| validate_readiness(ready, &expected_for_thread));
        let _ = tx.send(outcome);
    });

    match rx.recv_timeout(READY_TIMEOUT) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => Err(LaunchError::ReadinessTimeout {
            diagnostics: diagnostics.snapshot(),
        }),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(LaunchError::invalid_readiness(
            "readiness channel closed unexpectedly",
        )),
    }
}

struct StartupDiagnostics {
    secret: String,
    tail: Arc<Mutex<StderrTail>>,
    drained: mpsc::Receiver<()>,
}

impl StartupDiagnostics {
    fn spawn<R: Read + Send + 'static>(stderr: R, secret: String) -> Self {
        let tail = Arc::new(Mutex::new(StderrTail::default()));
        let stderr_tail = Arc::clone(&tail);
        let (drained_tx, drained_rx) = mpsc::channel();
        std::thread::spawn(move || drain_stderr(stderr, stderr_tail, drained_tx));
        Self {
            secret,
            tail,
            drained: drained_rx,
        }
    }

    fn snapshot(&self) -> Option<String> {
        snapshot_diagnostics(&self.tail, &self.secret)
    }

    fn finalize_failure(&self, child: &mut Child, error: LaunchError) -> LaunchError {
        self.wait_for_terminal_signal(child);
        error.with_diagnostics(self.snapshot())
    }

    fn wait_for_terminal_signal(&self, child: &mut Child) {
        let deadline = Instant::now() + TERMINAL_FAILURE_DIAGNOSTIC_WAIT;
        loop {
            match self.drained.try_recv() {
                Ok(()) | Err(mpsc::TryRecvError::Disconnected) => return,
                Err(mpsc::TryRecvError::Empty) => {}
            }

            match child.try_wait() {
                Ok(Some(_)) => {
                    let remaining = deadline.saturating_duration_since(Instant::now());
                    if remaining.is_zero() {
                        return;
                    }
                    let _ = self.drained.recv_timeout(remaining);
                    return;
                }
                Ok(None) => {}
                Err(_) => return,
            }

            if Instant::now() >= deadline {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

fn read_readiness<R: Read + Send + 'static>(stdout: R) -> Result<SidecarReady, LaunchError> {
    let mut reader = BufReader::new(stdout);
    let mut line = Vec::with_capacity(256);
    let read = reader.read_until(b'\n', &mut line)?;
    if read == 0 {
        return Err(LaunchError::invalid_readiness(
            "stdout closed before a readiness line arrived",
        ));
    }
    if line.len() > MAX_READY_LINE_BYTES {
        return Err(LaunchError::invalid_readiness(format!(
            "readiness line exceeded the {} byte limit",
            MAX_READY_LINE_BYTES
        )));
    }

    while line
        .last()
        .is_some_and(|byte| matches!(byte, b'\n' | b'\r'))
    {
        line.pop();
    }

    let ready: SidecarReady = serde_json::from_slice(&line)
        .map_err(|_| LaunchError::invalid_readiness("readiness payload was not valid JSON"))?;
    std::thread::spawn(move || drain_and_discard(reader));
    Ok(ready)
}

#[derive(Default)]
struct StderrTail {
    bytes: Vec<u8>,
}

impl StderrTail {
    fn push(&mut self, chunk: &[u8]) {
        if chunk.len() >= MAX_STDERR_TAIL_BYTES {
            self.bytes.clear();
            self.bytes
                .extend_from_slice(&chunk[chunk.len() - MAX_STDERR_TAIL_BYTES..]);
            return;
        }
        let overflow = self.bytes.len() + chunk.len();
        if overflow > MAX_STDERR_TAIL_BYTES {
            let to_drop = overflow - MAX_STDERR_TAIL_BYTES;
            self.bytes.drain(..to_drop);
        }
        self.bytes.extend_from_slice(chunk);
    }

    fn render(&self, secret: &str) -> Option<String> {
        let raw = String::from_utf8_lossy(&self.bytes);
        sanitize_diagnostics(&raw, secret)
    }
}

fn drain_stderr<R: Read>(stderr: R, tail: Arc<Mutex<StderrTail>>, drained: mpsc::Sender<()>) {
    let mut reader = BufReader::new(stderr);
    let mut buffer = [0_u8; 512];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(read) => {
                if let Ok(mut tail) = tail.lock() {
                    tail.push(&buffer[..read]);
                }
            }
        }
    }
    let _ = drained.send(());
}

fn snapshot_diagnostics(tail: &Arc<Mutex<StderrTail>>, secret: &str) -> Option<String> {
    tail.lock().ok().and_then(|tail| tail.render(secret))
}

fn drain_and_discard<R: Read>(reader: R) {
    let mut reader = BufReader::new(reader);
    let mut buffer = [0_u8; 4096];
    loop {
        match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(_) => {}
        }
    }
}

fn validate_readiness(
    ready: SidecarReady,
    expected: &SidecarBootstrapRequest,
) -> Result<SidecarReady, LaunchError> {
    if ready.status != "ready" {
        return Err(LaunchError::invalid_readiness(
            "status must be the literal \"ready\"",
        ));
    }
    if ready.protocol_version != PROTOCOL_VERSION {
        return Err(LaunchError::invalid_readiness(format!(
            "protocol_version must be {}",
            PROTOCOL_VERSION
        )));
    }
    if ready.listener_addr != expected.listener_addr {
        return Err(LaunchError::invalid_readiness(
            "listener_addr did not match the inherited socket address",
        ));
    }
    if ready.address_family != "ipv4" {
        return Err(LaunchError::invalid_readiness(
            "address_family must be the literal \"ipv4\"",
        ));
    }
    if !ready.inherited_socket {
        return Err(LaunchError::invalid_readiness(
            "inherited_socket must be true",
        ));
    }
    if ready.session_header_name != SESSION_HEADER_NAME {
        return Err(LaunchError::invalid_readiness(
            "session_header_name did not match X-Lyra-Session",
        ));
    }
    if ready.session_secret != expected.session_secret {
        return Err(LaunchError::invalid_readiness(
            "session_secret did not match the launch secret",
        ));
    }
    validate_api_base(&ready.api_base, &expected.listener_addr)?;
    Ok(ready)
}

fn validate_api_base(api_base: &str, expected_listener_addr: &str) -> Result<(), LaunchError> {
    if api_base == format!("http://{expected_listener_addr}") {
        return Ok(());
    }
    Err(LaunchError::invalid_readiness(
        "api_base did not match the inherited loopback listener",
    ))
}

fn sanitize_diagnostics(raw: &str, secret: &str) -> Option<String> {
    let home = std::env::var("HOME").unwrap_or_default();
    sanitize_diagnostics_with_home(raw, secret, &home)
}

fn sanitize_diagnostics_with_home(raw: &str, secret: &str, home: &str) -> Option<String> {
    let collapsed = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        return None;
    }
    let mut safe = collapsed;
    if !secret.is_empty() {
        safe = safe.replace(secret, "<secret>");
    }
    if !home.is_empty() {
        safe = safe.replace(home, "<home>");
    }
    if contains_sensitive_startup_text(&safe) {
        return Some("<redacted sensitive startup diagnostics>".to_string());
    }
    let truncated = safe
        .chars()
        .rev()
        .take(MAX_DIAGNOSTIC_CHARS)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<String>();
    Some(truncated)
}

fn contains_sensitive_startup_text(text: &str) -> bool {
    let lower = text.to_ascii_lowercase();
    let marker = [
        "authorization",
        "api_key",
        "api-key",
        "token=",
        "token:",
        "secret=",
        "secret:",
        "password=",
        "password:",
        "prompt=",
        "prompt:",
        "document=",
        "document:",
        "content=",
        "content:",
        "body=",
        "body:",
        "response=",
        "response:",
        "html=",
        "html:",
        "text=",
        "text:",
        "bearer ",
        "/private/",
        "/var/folders/",
        "/home/",
        "\\users\\",
    ]
    .iter()
    .any(|marker| lower.contains(marker));
    let credential_shaped = lower
        .split_whitespace()
        .any(|word| (word.starts_with("sk-") || word.starts_with("exa-")) && word.len() >= 12);
    marker || credential_shaped
}

fn resolve_sidecar_path(app: &AppHandle) -> Result<PathBuf, LaunchError> {
    let target_name = sidecar_filename();
    let resource_dir = app.path().resource_dir().ok();
    let current_exe_dir = std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(Path::to_path_buf));

    let mut candidates = Vec::new();
    if let Some(dir) = resource_dir {
        candidates.push(dir.join("lyra-backend").join(SIDECAR_NAME));
        candidates.push(
            dir.join("resources")
                .join("lyra-backend")
                .join(SIDECAR_NAME),
        );
        candidates.push(dir.join("binaries").join(&target_name));
        candidates.push(dir.join(&target_name));
    }
    if let Some(dir) = current_exe_dir {
        candidates.push(dir.join(&target_name));
        candidates.push(dir.join("../Resources").join("binaries").join(&target_name));
        candidates.push(dir.join("../Resources").join(&target_name));
    }
    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("binaries")
            .join(&target_name),
    );

    if let Some(found) = candidates.iter().find(|path| path.is_file()) {
        return Ok(found.clone());
    }

    Err(LaunchError::MissingSidecar {
        expected: candidates
            .into_iter()
            .map(|path| display_tail(&path))
            .collect(),
    })
}

fn display_tail(path: &Path) -> String {
    let parts = path
        .components()
        .rev()
        .take(4)
        .map(|component| component.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>();
    parts.into_iter().rev().collect::<Vec<_>>().join("/")
}

fn sidecar_filename() -> String {
    if cfg!(windows) {
        format!("{SIDECAR_NAME}-{}.exe", target_triple_suffix())
    } else {
        format!("{SIDECAR_NAME}-{}", target_triple_suffix())
    }
}

fn target_triple_suffix() -> &'static str {
    match (std::env::consts::ARCH, std::env::consts::OS) {
        ("aarch64", "macos") => "aarch64-apple-darwin",
        ("x86_64", "macos") => "x86_64-apple-darwin",
        ("x86_64", "linux") => "x86_64-unknown-linux-gnu",
        ("aarch64", "linux") => "aarch64-unknown-linux-gnu",
        ("x86_64", "windows") => "x86_64-pc-windows-msvc",
        ("aarch64", "windows") => "aarch64-pc-windows-msvc",
        _ => "unknown-target",
    }
}

fn random_secret_hex() -> Result<String, LaunchError> {
    let mut bytes = [0u8; 32];
    let mut file = File::open("/dev/urandom")?;
    file.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn lyra_data_dir() -> Option<PathBuf> {
    if let Some(configured) = std::env::var_os("LYRA_DATA_DIR") {
        return Some(PathBuf::from(configured));
    }
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)?;
    if cfg!(target_os = "macos") {
        return Some(
            home.join("Library")
                .join("Application Support")
                .join("Lyra"),
        );
    }
    if cfg!(target_os = "windows") {
        let roaming = std::env::var_os("APPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Roaming"));
        return Some(roaming.join("Lyra"));
    }
    let data = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local").join("share"));
    Some(data.join("Lyra"))
}

fn import_selections_dir() -> Result<PathBuf, LaunchError> {
    let data_dir = lyra_data_dir().ok_or(LaunchError::Import(
        "the application-support directory is unavailable",
    ))?;
    let parent = data_dir.parent().ok_or(LaunchError::Import(
        "the application-support directory is invalid",
    ))?;
    Ok(parent.join(".desktop-import-selections"))
}

fn record_import_selection(path: &Path) -> Result<ImportSelectionPayload, LaunchError> {
    let directory = import_selections_dir()?;
    let token = random_secret_hex()?;
    write_import_selection(path, &directory, token)
}

fn write_import_selection(
    path: &Path,
    directory: &Path,
    token: String,
) -> Result<ImportSelectionPayload, LaunchError> {
    let selected = path
        .canonicalize()
        .map_err(|_| LaunchError::Import("the selected folder could not be reopened"))?;
    if !selected.is_dir() {
        return Err(LaunchError::Import("the selected item was not a directory"));
    }
    let selected_text = selected.to_str().ok_or(LaunchError::Import(
        "the selected folder name is unsupported",
    ))?;
    let label = selected
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.trim().is_empty())
        .unwrap_or("Selected Lyra folder")
        .to_string();
    fs::create_dir_all(directory)?;
    harden_startup_log_dir(directory)?;

    let final_path = directory.join(format!("{token}.json"));
    let temporary = directory.join(format!(".{token}.tmp"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    harden_startup_log_file(&temporary)?;
    serde_json::to_writer(
        &mut file,
        &ImportSelectionRecord {
            path: selected_text,
            label: &label,
        },
    )?;
    file.write_all(b"\n")?;
    file.sync_all()?;
    fs::rename(&temporary, &final_path)?;
    harden_startup_log_file(&final_path)?;

    Ok(ImportSelectionPayload {
        selection_token: token,
        label,
    })
}

fn run_import_publication(sidecar_path: &Path) -> Result<(), LaunchError> {
    let mut command = Command::new(sidecar_path);
    command
        .arg("--publish-desktop-import")
        .env("LYRA_PACKAGED", "1");
    let output = bounded_process::run(&mut command, Duration::from_secs(60)).map_err(|_| {
        LaunchError::Import(
            "import publication did not finish; restart Lyra to recover the staged import",
        )
    })?;
    let status = output.status;
    if status.success() {
        Ok(())
    } else {
        Err(LaunchError::Import(
            "the staged data could not be published; restart Lyra to recover the staged import",
        ))
    }
}

fn child_is_running(child: &mut Child) -> Result<bool, LaunchError> {
    Ok(child.try_wait()?.is_none())
}

fn stop_backend(backend: &mut ManagedBackend) -> Result<(), LaunchError> {
    stop_child(
        &mut backend.child,
        Some(&backend.bootstrap),
        Some(&backend.sidecar_path),
    )
}

fn stop_child(
    child: &mut Child,
    bootstrap: Option<&BootstrapPayload>,
    sidecar_path: Option<&Path>,
) -> Result<(), LaunchError> {
    if child.try_wait().ok().flatten().is_some() {
        return reclaim_helpers(sidecar_path);
    }
    if let Some(bootstrap) = bootstrap {
        if request_graceful_shutdown(bootstrap).is_ok()
            && wait_for_exit(child, SHUTDOWN_GRACE_TIMEOUT)
        {
            return reclaim_helpers(sidecar_path);
        }
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    {
        if wait_for_exit(child, SHUTDOWN_GRACE_TIMEOUT) {
            return reclaim_helpers(sidecar_path);
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    reclaim_helpers(sidecar_path)
}

fn wait_for_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(_) => return false,
        }
    }
    false
}

fn request_graceful_shutdown(bootstrap: &BootstrapPayload) -> Result<(), LaunchError> {
    let status = loopback_request(
        bootstrap,
        "POST",
        "/api/health/shutdown",
        true,
        Duration::from_secs(2),
    )?;
    if status == 202 {
        return Ok(());
    }
    Err(LaunchError::invalid_readiness(format!(
        "graceful shutdown request failed: HTTP {status}"
    )))
}

fn api_port(api_base: &str) -> Option<u16> {
    api_base
        .strip_prefix("http://127.0.0.1:")
        .and_then(|port| port.parse::<u16>().ok())
}

fn backend_is_ready(bootstrap: &BootstrapPayload) -> bool {
    loopback_request(
        bootstrap,
        "GET",
        "/api/health/ready",
        false,
        Duration::from_secs(1),
    )
    .is_ok_and(|status| status == 200)
}

fn loopback_request(
    bootstrap: &BootstrapPayload,
    method: &str,
    path: &str,
    include_client_header: bool,
    timeout: Duration,
) -> Result<u16, LaunchError> {
    let port = api_port(&bootstrap.api_base).ok_or_else(|| {
        LaunchError::invalid_readiness("api_base did not contain a loopback port")
    })?;
    let mut stream = TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let mut request = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nContent-Length: 0\r\n{}: {}\r\n",
        bootstrap.session_header_name, bootstrap.session_secret
    );
    if include_client_header {
        request.push_str(&format!("{LOOPBACK_CLIENT_HEADER_NAME}: desktop-shell\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes())?;
    stream.flush()?;
    let mut status_line = Vec::with_capacity(64);
    let mut reader = BufReader::new(stream).take((MAX_HTTP_STATUS_LINE_BYTES + 1) as u64);
    let read = reader.read_until(b'\n', &mut status_line)?;
    if read == 0 || status_line.len() > MAX_HTTP_STATUS_LINE_BYTES {
        return Err(LaunchError::invalid_readiness(
            "loopback request returned an invalid HTTP status line",
        ));
    }
    let status_line = String::from_utf8(status_line)
        .map_err(|_| LaunchError::invalid_readiness("loopback response status was not UTF-8"))?;
    parse_status_code(&status_line)
        .ok_or_else(|| LaunchError::invalid_readiness("loopback request returned no HTTP status"))
}

fn parse_status_code(status_line: &str) -> Option<u16> {
    status_line
        .split_whitespace()
        .nth(1)
        .and_then(|segment| segment.parse::<u16>().ok())
}

fn reclaim_helpers(sidecar_path: Option<&Path>) -> Result<(), LaunchError> {
    let Some(sidecar_path) = sidecar_path else {
        return Ok(());
    };
    let mut sidecar_command = Command::new(sidecar_path);
    sidecar_command
        .arg("--reclaim-helpers")
        .env("LYRA_PACKAGED", "1");
    let primary_error = match run_reclaim_command(&mut sidecar_command, "") {
        Ok(()) => return Ok(()),
        Err(detail) => detail,
    };
    if !looks_like_dev_sidecar(sidecar_path) {
        return Err(LaunchError::HelperReclaim {
            diagnostics: Some(primary_error),
        });
    }
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf);
    let Some(repo_root) = repo_root else {
        return Err(LaunchError::HelperReclaim {
            diagnostics: Some(primary_error),
        });
    };
    let mut command = Command::new("python3");
    command
        .arg("-m")
        .arg("backend.llm.helper_reclaim")
        .current_dir(repo_root);
    run_reclaim_command(&mut command, "").map_err(|detail| LaunchError::HelperReclaim {
        diagnostics: Some(detail),
    })
}

fn run_reclaim_command(command: &mut Command, secret: &str) -> Result<(), String> {
    let output = bounded_process::run(command, Duration::from_secs(10))
        .map_err(|_| "helper reclaim command could not be started".to_string())?;
    if output.status.success() {
        return Ok(());
    }
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stdout = String::from_utf8_lossy(&output.stdout);
    let detail = sanitize_diagnostics(
        if stderr.trim().is_empty() {
            stdout.as_ref()
        } else {
            stderr.as_ref()
        },
        secret,
    )
    .unwrap_or_else(|| "helper reclaim failed".to_string());
    Err(detail)
}

fn looks_like_dev_sidecar(sidecar_path: &Path) -> bool {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    sidecar_path.starts_with(manifest_dir)
}

fn focus_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn log_event(message: &str) {
    let console = sanitize_log_message(message, 160);
    eprintln!("[lyra-desktop] {console}");
    let _ = append_startup_log(message);
}

fn sanitize_log_message(message: &str, limit: usize) -> String {
    sanitize_diagnostics(message, "")
        .unwrap_or_default()
        .chars()
        .take(limit)
        .collect()
}

fn append_startup_log(message: &str) -> Result<(), std::io::Error> {
    let Some(path) = startup_log_path() else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
        harden_startup_log_dir(parent)?;
    }
    rotate_startup_log(&path)?;
    let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
    harden_startup_log_file(&path)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let line = sanitize_log_message(message, MAX_LOG_EVENT_CHARS);
    writeln!(file, "{now} {line}")?;
    Ok(())
}

fn startup_log_path() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("LYRA_LOGS_DIR") {
        return Some(PathBuf::from(path).join(STARTUP_LOG_NAME));
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))?;
    let home = PathBuf::from(home);
    if cfg!(target_os = "macos") {
        return Some(
            home.join("Library")
                .join("Logs")
                .join("Lyra")
                .join(STARTUP_LOG_NAME),
        );
    }
    if cfg!(target_os = "windows") {
        let local = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join("AppData").join("Local"));
        return Some(local.join("Lyra").join("Logs").join(STARTUP_LOG_NAME));
    }
    let state = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".local").join("state"));
    Some(state.join("Lyra").join("logs").join(STARTUP_LOG_NAME))
}

fn rotate_startup_log(path: &Path) -> Result<(), std::io::Error> {
    let size = match fs::metadata(path) {
        Ok(metadata) => metadata.len(),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(err) => return Err(err),
    };
    if size < STARTUP_LOG_ROTATE_BYTES {
        return Ok(());
    }
    for index in (1..=STARTUP_LOG_BACKUPS).rev() {
        let source = if index == 1 {
            path.to_path_buf()
        } else {
            path.with_extension(format!("log.{}", index - 1))
        };
        let target = path.with_extension(format!("log.{index}"));
        if source.is_file() {
            let _ = fs::remove_file(&target);
            fs::rename(source, target)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn harden_startup_log_dir(path: &Path) -> Result<(), std::io::Error> {
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
}

#[cfg(not(unix))]
fn harden_startup_log_dir(_path: &Path) -> Result<(), std::io::Error> {
    Ok(())
}

#[cfg(unix)]
fn harden_startup_log_file(path: &Path) -> Result<(), std::io::Error> {
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
}

#[cfg(not(unix))]
fn harden_startup_log_file(_path: &Path) -> Result<(), std::io::Error> {
    Ok(())
}

#[cfg(unix)]
use std::os::fd::AsRawFd;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

#[cfg(unix)]
struct InheritedFdGuard {
    fd: i32,
    previous_flags: i32,
}

#[cfg(unix)]
impl InheritedFdGuard {
    fn for_listener(listener: &TcpListener) -> Result<Self, LaunchError> {
        let fd = listener.as_raw_fd();
        let previous_flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if previous_flags < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let clear_close_on_exec = previous_flags & !libc::FD_CLOEXEC;
        let set_result = unsafe { libc::fcntl(fd, libc::F_SETFD, clear_close_on_exec) };
        if set_result < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        Ok(Self { fd, previous_flags })
    }
}

#[cfg(unix)]
impl Drop for InheritedFdGuard {
    fn drop(&mut self) {
        unsafe {
            libc::fcntl(self.fd, libc::F_SETFD, self.previous_flags);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const STARTUP_FIXTURE: &str = r#"
import json
import os
import sys
import time

mode = os.environ["LYRA_FIXTURE_MODE"]
delay = int(os.environ.get("LYRA_FIXTURE_DELAY_MS", "0")) / 1000.0
stderr_text = os.environ.get("LYRA_FIXTURE_STDERR", "")

def pause():
    if delay > 0:
        time.sleep(delay)

if mode == "broken_pipe":
    os.close(0)
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()
    pause()
    raise SystemExit(1)

if mode == "stdout_close_then_stderr":
    os.close(1)
    sys.stdin.readline()
    pause()
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()
    raise SystemExit(1)

if mode == "stderr_held_open":
    os.close(1)
    sys.stdin.readline()
    if stderr_text:
        sys.stderr.write(stderr_text)
        sys.stderr.flush()
    time.sleep(2)
    raise SystemExit(1)

if mode == "ready":
    sys.stdin.readline()
    readiness = {
        "status": "ready",
        "protocol_version": 1,
        "api_base": "http://" + os.environ["LYRA_FIXTURE_LISTENER"],
        "listener_addr": os.environ["LYRA_FIXTURE_LISTENER"],
        "address_family": "ipv4",
        "inherited_socket": True,
        "session_header_name": os.environ["LYRA_FIXTURE_HEADER"],
        "session_secret": os.environ["LYRA_FIXTURE_SECRET"],
    }
    sys.stdout.write(json.dumps(readiness) + "\n")
    sys.stdout.flush()
    pause()
    raise SystemExit(0)

raise SystemExit("unsupported startup fixture mode")
"#;

    fn bootstrap_for_port(port: u16) -> BootstrapPayload {
        BootstrapPayload {
            protocol_version: PROTOCOL_VERSION,
            api_base: format!("http://127.0.0.1:{port}"),
            session_header_name: SESSION_HEADER_NAME,
            session_secret: "a".repeat(64),
        }
    }

    fn serve_probe_response(
        listener: TcpListener,
        response: &'static [u8],
    ) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut request = String::new();
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                    break;
                }
                request.push_str(&line);
            }
            assert!(request.starts_with("GET /api/health/ready HTTP/1.1\r\n"));
            assert!(request.contains(&format!("{SESSION_HEADER_NAME}: {}", "a".repeat(64))));
            let mut stream = reader.into_inner();
            stream.write_all(response).unwrap();
        })
    }

    fn expected_bootstrap() -> SidecarBootstrapRequest {
        SidecarBootstrapRequest {
            protocol_version: PROTOCOL_VERSION,
            socket_fd: 4,
            parent_pid: 7,
            listener_addr: "127.0.0.1:43123".to_string(),
            session_header_name: SESSION_HEADER_NAME,
            session_secret: "a".repeat(64),
        }
    }

    fn python_command() -> &'static str {
        ["python3", "python"]
            .into_iter()
            .find(|candidate| {
                Command::new(candidate)
                    .arg("--version")
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .is_ok()
            })
            .expect("python is required for startup fixture tests")
    }

    fn spawn_startup_fixture(
        bootstrap: &SidecarBootstrapRequest,
        mode: &str,
        stderr_text: &str,
        delay_ms: u64,
    ) -> Child {
        let mut command = Command::new(python_command());
        command
            .arg("-c")
            .arg(STARTUP_FIXTURE)
            .env("LYRA_FIXTURE_MODE", mode)
            .env("LYRA_FIXTURE_STDERR", stderr_text)
            .env("LYRA_FIXTURE_DELAY_MS", delay_ms.to_string())
            .env("LYRA_FIXTURE_LISTENER", &bootstrap.listener_addr)
            .env("LYRA_FIXTURE_HEADER", bootstrap.session_header_name)
            .env("LYRA_FIXTURE_SECRET", &bootstrap.session_secret)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        command.spawn().unwrap()
    }

    #[test]
    fn parses_readiness_payload() {
        let ready: SidecarReady = serde_json::from_str(
            r#"{
                "status":"ready",
                "protocol_version":1,
                "api_base":"http://127.0.0.1:43123",
                "listener_addr":"127.0.0.1:43123",
                "address_family":"ipv4",
                "inherited_socket":true,
                "session_header_name":"X-Lyra-Session",
                "session_secret":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            }"#,
        )
        .unwrap();

        assert_eq!(
            ready,
            SidecarReady {
                status: "ready".to_string(),
                protocol_version: PROTOCOL_VERSION,
                api_base: "http://127.0.0.1:43123".to_string(),
                listener_addr: "127.0.0.1:43123".to_string(),
                address_family: "ipv4".to_string(),
                inherited_socket: true,
                session_header_name: SESSION_HEADER_NAME.to_string(),
                session_secret: "a".repeat(64),
            }
        );
    }

    #[test]
    fn rejects_readiness_with_unknown_fields() {
        let error = read_readiness(
            br#"{"status":"ready","protocol_version":1,"api_base":"http://127.0.0.1:43123","listener_addr":"127.0.0.1:43123","address_family":"ipv4","inherited_socket":true,"session_header_name":"X-Lyra-Session","session_secret":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","extra":true}
"#
            .as_slice(),
        )
        .unwrap_err();

        assert!(error.to_string().contains("not valid JSON"));
    }

    #[test]
    fn rejects_non_matching_listener_api_base() {
        let error = validate_api_base("http://127.0.0.1:8000", "127.0.0.1:43123").unwrap_err();

        assert!(error.to_string().contains("inherited loopback listener"));
    }

    #[test]
    fn validates_ready_contract() {
        let bootstrap = expected_bootstrap();
        let ready = SidecarReady {
            status: "ready".to_string(),
            protocol_version: PROTOCOL_VERSION,
            api_base: "http://127.0.0.1:43123".to_string(),
            listener_addr: "127.0.0.1:43123".to_string(),
            address_family: "ipv4".to_string(),
            inherited_socket: true,
            session_header_name: SESSION_HEADER_NAME.to_string(),
            session_secret: "a".repeat(64),
        };

        assert_eq!(
            validate_readiness(ready, &bootstrap).unwrap().listener_addr,
            bootstrap.listener_addr
        );
    }

    #[test]
    fn bootstrap_payload_uses_explicit_webview_fields() {
        let payload = serde_json::to_value(BootstrapPayload {
            protocol_version: PROTOCOL_VERSION,
            api_base: "http://127.0.0.1:43123".to_string(),
            session_header_name: SESSION_HEADER_NAME,
            session_secret: "a".repeat(64),
        })
        .unwrap();

        assert_eq!(
            payload,
            serde_json::json!({
                "protocolVersion": 1,
                "apiBase": "http://127.0.0.1:43123",
                "sessionHeaderName": "X-Lyra-Session",
                "sessionSecret": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            })
        );
    }

    #[test]
    fn native_import_selection_exposes_only_an_opaque_token_and_label() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("lyra-import-picker-{}-{nonce}", std::process::id()));
        let selected = root.join("Old Lyra 学校");
        let records = root.join("records");
        fs::create_dir_all(&selected).unwrap();

        let payload = write_import_selection(&selected, &records, "d".repeat(64)).unwrap();
        let webview_value = serde_json::to_value(&payload).unwrap();
        assert_eq!(
            webview_value,
            serde_json::json!({
                "selectionToken": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                "label": "Old Lyra 学校",
            })
        );
        assert!(!webview_value
            .to_string()
            .contains(&selected.to_string_lossy().to_string()));

        let record_path = records.join(format!("{}.json", "d".repeat(64)));
        let record: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&record_path).unwrap()).unwrap();
        assert_eq!(
            record["path"],
            selected.canonicalize().unwrap().to_string_lossy().as_ref()
        );
        #[cfg(unix)]
        assert_eq!(
            fs::metadata(&record_path).unwrap().permissions().mode() & 0o777,
            0o600
        );

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cached_backend_requires_an_authenticated_ready_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let bootstrap = bootstrap_for_port(listener.local_addr().unwrap().port());
        let server = serve_probe_response(
            listener,
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );

        assert!(backend_is_ready(&bootstrap));
        server.join().unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let bootstrap = bootstrap_for_port(listener.local_addr().unwrap().port());
        let server = serve_probe_response(
            listener,
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
        );

        assert!(!backend_is_ready(&bootstrap));
        server.join().unwrap();
    }

    #[test]
    fn cached_backend_rejects_an_oversized_status_line() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let bootstrap = bootstrap_for_port(listener.local_addr().unwrap().port());
        let oversized = Box::leak(vec![b'A'; MAX_HTTP_STATUS_LINE_BYTES + 1].into_boxed_slice());
        let server = serve_probe_response(listener, oversized);

        assert!(!backend_is_ready(&bootstrap));
        server.join().unwrap();
    }

    #[test]
    fn redacts_unicode_diagnostics() {
        // Passes the home directory explicitly: mutating the process-global
        // HOME here races with concurrently running redaction tests.
        let detail = sanitize_diagnostics_with_home(
            "prefix /Users/private/Library/Logs aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa café",
            &"a".repeat(64),
            "/Users/private",
        )
        .unwrap();

        assert!(detail.contains("<home>"));
        assert!(detail.contains("<secret>"));
        assert!(detail.contains("café"));
    }

    #[test]
    fn redacts_sensitive_startup_diagnostics() {
        let detail = sanitize_diagnostics("Authorization: Bearer token", "").unwrap();

        assert_eq!(detail, "<redacted sensitive startup diagnostics>");
        assert_eq!(
            sanitize_diagnostics("provider failed with sk-proj-sensitivevalue", "").unwrap(),
            "<redacted sensitive startup diagnostics>"
        );
        assert_eq!(
            sanitize_diagnostics("failed at /private/var/tmp/student/course.db", "").unwrap(),
            "<redacted sensitive startup diagnostics>"
        );
    }

    #[test]
    fn formats_target_specific_sidecar_name() {
        let filename = sidecar_filename();

        assert!(filename.starts_with("lyra-backend-"));
        assert!(!filename.contains(' '));
    }

    #[test]
    fn write_bootstrap_request_attaches_traceback_to_broken_pipe_failures() {
        let bootstrap = expected_bootstrap();
        let mut child = spawn_startup_fixture(
            &bootstrap,
            "broken_pipe",
            "Traceback: startup exploded before stdin\n",
            125,
        );
        let stderr = child.stderr.take().unwrap();
        let diagnostics = StartupDiagnostics::spawn(stderr, bootstrap.session_secret.clone());

        let deadline = Instant::now() + Duration::from_secs(1);
        while Instant::now() < deadline {
            if diagnostics
                .snapshot()
                .is_some_and(|detail| detail.contains("Traceback: startup exploded before stdin"))
            {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }

        let write_error = write_bootstrap_request(&mut child, &bootstrap).unwrap_err();
        let error = diagnostics.finalize_failure(&mut child, write_error);
        let _ = child.wait();
        let message = error.to_string();

        assert!(message.to_ascii_lowercase().contains("broken pipe"));
        assert!(message.contains("Traceback: startup exploded before stdin"));
    }

    #[test]
    fn bootstrap_child_waits_for_delayed_stderr_after_stdout_closes() {
        let bootstrap = expected_bootstrap();
        let mut child = spawn_startup_fixture(
            &bootstrap,
            "stdout_close_then_stderr",
            "late stderr after stdout closed\n",
            125,
        );

        let error = bootstrap_child(&mut child, &bootstrap).unwrap_err();
        let _ = child.wait();
        let message = error.to_string();

        assert!(message.contains("stdout closed before a readiness line arrived"));
        assert!(message.contains("late stderr after stdout closed"));
    }

    #[test]
    fn bootstrap_child_bounds_wait_for_a_child_that_holds_stderr_open() {
        let bootstrap = expected_bootstrap();
        let mut child =
            spawn_startup_fixture(&bootstrap, "stderr_held_open", "partial traceback\n", 0);

        let started = Instant::now();
        let error = bootstrap_child(&mut child, &bootstrap).unwrap_err();
        let elapsed = started.elapsed();
        let _ = child.kill();
        let _ = child.wait();

        assert!(elapsed < Duration::from_secs(2));
        assert!(error.to_string().contains("partial traceback"));
    }

    #[test]
    fn bootstrap_child_redacts_secrets_in_child_stderr() {
        let bootstrap = expected_bootstrap();
        let home = std::env::var("HOME").unwrap_or_else(|_| "/Users/private".to_string());
        let stderr_text = format!(
            "tail {home}/Library/Logs {} safe-context\n",
            bootstrap.session_secret
        );
        let mut child =
            spawn_startup_fixture(&bootstrap, "stdout_close_then_stderr", &stderr_text, 50);

        let error = bootstrap_child(&mut child, &bootstrap).unwrap_err();
        let _ = child.wait();
        let message = error.to_string();

        assert!(message.contains("<secret>"));
        assert!(!message.contains(&bootstrap.session_secret));
        if !home.is_empty() {
            assert!(message.contains("<home>"));
            assert!(!message.contains(&home));
        }
    }

    #[test]
    fn bootstrap_child_supports_retry_after_a_failed_attempt() {
        let bootstrap = expected_bootstrap();
        let mut failed = spawn_startup_fixture(
            &bootstrap,
            "stdout_close_then_stderr",
            "first failure\n",
            50,
        );

        let first_error = bootstrap_child(&mut failed, &bootstrap).unwrap_err();
        let _ = failed.wait();
        assert!(first_error.to_string().contains("first failure"));

        let mut retried = spawn_startup_fixture(&bootstrap, "ready", "", 0);
        let ready = bootstrap_child(&mut retried, &bootstrap).unwrap();
        let _ = retried.wait();

        assert_eq!(ready.listener_addr, bootstrap.listener_addr);
        assert_eq!(ready.session_secret, bootstrap.session_secret);
    }

    #[test]
    fn failed_helper_reclaim_is_a_terminal_invariant() {
        let mut command = Command::new(python_command());
        command.arg("-c").arg(
            "import sys; print('{\"status\":\"error\",\"services\":[{\"service\":\"reranking\",\"after\":\"live\",\"ok\":false}]}'); sys.exit(1)",
        );

        let detail = run_reclaim_command(&mut command, "").unwrap_err();
        let error = LaunchError::HelperReclaim {
            diagnostics: Some(detail),
        };
        let message = error.to_string();

        assert!(message.contains("owned helper reclamation invariant failed"));
        assert!(message.contains("\"after\":\"live\""));
    }
}

#[cfg(all(test, unix))]
mod original_copy_tests {
    use super::*;
    use std::os::unix::fs::{symlink, PermissionsExt};

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir()
                .join(format!("lyra-original-{}", random_secret_hex().unwrap()));
            fs::create_dir(&root).unwrap();
            Self(root)
        }
        fn destination(&self) -> PathBuf {
            self.0.join("original.pdf")
        }
        fn assert_no_temporary_files(&self) {
            assert!(fs::read_dir(&self.0).unwrap().all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".lyra-original-")));
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            ORIGINAL_COPY_HOOK.with(|hook| *hook.borrow_mut() = None);
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn inject_failure(failure: OriginalCopyStage) {
        ORIGINAL_COPY_HOOK.with(|hook| {
            *hook.borrow_mut() = Some(Box::new(move |stage, path| {
                if stage == failure {
                    // Inspect the real private sibling at the actual production fault point.
                    let sibling = fs::read_dir(path.parent().unwrap())
                        .unwrap()
                        .map(|entry| entry.unwrap().path())
                        .find(|path| {
                            path.file_name()
                                .unwrap()
                                .to_string_lossy()
                                .starts_with(".lyra-original-")
                        });
                    if stage == OriginalCopyStage::DuringWrite {
                        assert_eq!(fs::metadata(sibling.unwrap()).unwrap().len(), 4096);
                    }
                    Err(std::io::Error::other("injected native failure"))
                } else {
                    Ok(())
                }
            }))
        });
    }

    #[test]
    fn saves_exact_binary_bytes_privately_and_cleans_up() {
        let fixture = Fixture::new();
        let bytes = b"%PDF\0\xff original bytes\r\n";
        write_original_copy(&fixture.destination(), bytes).unwrap();
        assert_eq!(fs::read(fixture.destination()).unwrap(), bytes);
        assert_eq!(
            fs::metadata(fixture.destination())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        fixture.assert_no_temporary_files();
    }

    #[test]
    fn prepublication_failures_leave_absent_destination_absent_and_clean_up() {
        for stage in [
            OriginalCopyStage::BeforeWrite,
            OriginalCopyStage::DuringWrite,
            OriginalCopyStage::Flush,
            OriginalCopyStage::Publication,
        ] {
            let fixture = Fixture::new();
            inject_failure(stage);
            let error = write_original_copy(&fixture.destination(), &[42; 8192]).unwrap_err();
            assert!(
                matches!(error, OriginalCopyError::BeforePublication(_)),
                "{stage:?}: {error}"
            );
            assert!(!fixture.destination().exists(), "{stage:?}");
            fixture.assert_no_temporary_files();
        }
    }

    #[test]
    fn no_overwrite_preserves_existing_bytes_before_every_fault_point() {
        for stage in [
            OriginalCopyStage::BeforeWrite,
            OriginalCopyStage::DuringWrite,
            OriginalCopyStage::Flush,
            OriginalCopyStage::Publication,
        ] {
            let fixture = Fixture::new();
            fs::write(fixture.destination(), b"existing\0\xff bytes").unwrap();
            inject_failure(stage);
            let error = write_original_copy(&fixture.destination(), &[42; 8192]).unwrap_err();
            // Existing files are rejected before any write/flush/publication is attempted.
            assert!(matches!(error, OriginalCopyError::DestinationExists));
            assert!(error.to_string().contains("Choose another filename"));
            assert_eq!(
                fs::read(fixture.destination()).unwrap(),
                b"existing\0\xff bytes"
            );
            fixture.assert_no_temporary_files();
        }
    }

    #[test]
    fn no_overwrite_preserves_existing_file_and_allows_another_filename() {
        let fixture = Fixture::new();
        fs::write(fixture.destination(), b"existing").unwrap();
        assert!(matches!(
            write_original_copy(&fixture.destination(), b"new"),
            Err(OriginalCopyError::DestinationExists)
        ));
        write_original_copy(&fixture.0.join("another.pdf"), b"new").unwrap();
        assert_eq!(fs::read(fixture.destination()).unwrap(), b"existing");
        assert_eq!(fs::read(fixture.0.join("another.pdf")).unwrap(), b"new");
        fixture.assert_no_temporary_files();
    }

    #[test]
    fn rejects_symlinks_directories_and_special_files_without_opening_them() {
        let fixture = Fixture::new();
        let target = fixture.0.join("target");
        fs::write(&target, b"untouched").unwrap();
        for exists in [true, false] {
            let link = fixture.0.join(if exists { "link" } else { "dangling" });
            symlink(
                if exists {
                    target.clone()
                } else {
                    fixture.0.join("absent")
                },
                &link,
            )
            .unwrap();
            assert!(matches!(
                write_original_copy(&link, b"new"),
                Err(OriginalCopyError::DestinationExists)
            ));
            assert!(link.symlink_metadata().unwrap().file_type().is_symlink());
        }
        assert_eq!(fs::read(&target).unwrap(), b"untouched");
        assert!(!fixture.0.join("absent").exists());
        assert!(write_original_copy(&fixture.0, b"new").is_err());
        let fifo = fixture.0.join("fifo");
        use std::os::unix::ffi::OsStrExt;
        let fifo_name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        // SAFETY: the pathname is a live NUL-terminated CString.
        assert_eq!(unsafe { libc::mkfifo(fifo_name.as_ptr(), 0o600) }, 0);
        assert!(matches!(
            write_original_copy(&fifo, b"new"),
            Err(OriginalCopyError::DestinationExists)
        ));
        fixture.assert_no_temporary_files();
    }

    #[test]
    fn publication_never_clobbers_racing_file_symlink_or_directory() {
        for entry in ["file", "symlink", "directory"] {
            let fixture = Fixture::new();
            ORIGINAL_COPY_HOOK.with(|hook| {
                *hook.borrow_mut() = Some(Box::new(move |stage, path| {
                    if stage == OriginalCopyStage::Publication {
                        match entry {
                            "file" => fs::write(path, b"racing bytes")?,
                            "symlink" => symlink("absent-target", path)?,
                            "directory" => fs::create_dir(path)?,
                            _ => unreachable!(),
                        }
                    }
                    Ok(())
                }))
            });
            assert!(matches!(
                write_original_copy(&fixture.destination(), b"new"),
                Err(OriginalCopyError::DestinationExists)
            ));
            match entry {
                "file" => assert_eq!(fs::read(fixture.destination()).unwrap(), b"racing bytes"),
                "symlink" => assert_eq!(
                    fs::read_link(fixture.destination()).unwrap(),
                    Path::new("absent-target")
                ),
                "directory" => assert!(fixture.destination().is_dir()),
                _ => unreachable!(),
            }
            fixture.assert_no_temporary_files();
        }
    }

    #[test]
    fn postpublication_errors_report_saved_bytes_and_clean_up_without_rollback() {
        for stage in [OriginalCopyStage::Cleanup, OriginalCopyStage::Durability] {
            let fixture = Fixture::new();
            inject_failure(stage);
            let error =
                write_original_copy(&fixture.destination(), b"published bytes").unwrap_err();
            assert!(matches!(error, OriginalCopyError::Published(_)));
            assert!(error.user_message().contains("was saved"));
            assert!(!error.user_message().contains("could not be saved"));
            assert_eq!(fs::read(fixture.destination()).unwrap(), b"published bytes");
            fixture.assert_no_temporary_files();
        }
    }

    #[test]
    fn parent_rename_does_not_redirect_publication_or_cleanup() {
        let fixture = Fixture::new();
        let parent = fixture.0.join("parent");
        let moved = fixture.0.join("moved");
        fs::create_dir(&parent).unwrap();
        let moved_for_hook = moved.clone();
        ORIGINAL_COPY_HOOK.with(|hook| {
            *hook.borrow_mut() = Some(Box::new(move |stage, path| {
                if stage == OriginalCopyStage::Publication {
                    fs::rename(path.parent().unwrap(), &moved_for_hook)?;
                    fs::create_dir(path.parent().unwrap())?;
                    fs::write(path, b"different directory")?;
                }
                Ok(())
            }))
        });
        write_original_copy(&parent.join("original.pdf"), b"saved bytes").unwrap();
        assert_eq!(
            fs::read(parent.join("original.pdf")).unwrap(),
            b"different directory"
        );
        assert_eq!(
            fs::read(moved.join("original.pdf")).unwrap(),
            b"saved bytes"
        );
        assert_eq!(fs::read_dir(&moved).unwrap().count(), 1);
    }
}
