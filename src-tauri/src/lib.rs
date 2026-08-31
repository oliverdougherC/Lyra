mod external_navigation;

use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
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
}

impl From<LaunchError> for CommandError {
    fn from(value: LaunchError) -> Self {
        Self {
            message: value.to_string(),
        }
    }
}

impl From<external_navigation::ExternalNavigationError> for CommandError {
    fn from(value: external_navigation::ExternalNavigationError) -> Self {
        Self {
            message: value.to_string(),
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

#[derive(Default)]
struct AppState {
    lifecycle: Mutex<Lifecycle>,
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
    Io(std::io::Error),
    Json(serde_json::Error),
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
    Import(&'static str),
    Poisoned,
}

impl fmt::Display for LaunchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(err) => write!(f, "desktop shell I/O failed: {err}"),
            Self::Json(err) => write!(f, "desktop shell JSON failed: {err}"),
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
        Self::Io(value)
    }
}

impl From<serde_json::Error> for LaunchError {
    fn from(value: serde_json::Error) -> Self {
        Self::Json(value)
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

        if !force_restart {
            if let Some(existing) = lifecycle.backend.as_mut() {
                if child_is_running(&mut existing.child)? && backend_is_ready(&existing.bootstrap) {
                    return Ok(existing.bootstrap.clone());
                }
                log_event("backend health probe failed or process exited; preparing a restart");
                stop_backend(existing);
                lifecycle.backend = None;
            }
        }

        if force_restart {
            if let Some(existing) = lifecycle.backend.as_mut() {
                log_event("retry_backend requested; recycling owned backend");
                stop_backend(existing);
            }
            lifecycle.backend = None;
        }

        let managed = launch_backend(app)?;
        let bootstrap = managed.bootstrap.clone();
        lifecycle.backend = Some(managed);
        Ok(bootstrap)
    }

    fn shutdown(&self) {
        let Ok(mut lifecycle) = self.lifecycle.lock() else {
            return;
        };
        if let Some(existing) = lifecycle.backend.as_mut() {
            log_event("desktop shell is stopping its owned backend");
            stop_backend(existing);
        }
        lifecycle.backend = None;
    }

    fn publish_import(&self, app: &AppHandle) -> Result<BootstrapPayload, LaunchError> {
        let mut lifecycle = self.lifecycle.lock().map_err(|_| LaunchError::Poisoned)?;
        let sidecar_path = if let Some(existing) = lifecycle.backend.as_mut() {
            let path = existing.sidecar_path.clone();
            log_event("desktop import publication is stopping the owned backend");
            stop_backend(existing);
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
fn desktop_bootstrap(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    state.ensure_backend(&app, false).map_err(|error| {
        log_event(&format!("backend bootstrap failed: {error}"));
        error.into()
    })
}

#[tauri::command]
fn retry_backend(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    state.ensure_backend(&app, true).map_err(|error| {
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
fn publish_desktop_import(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<BootstrapPayload, CommandError> {
    state.publish_import(&app).map_err(|error| {
        log_event(&format!("desktop import publication failed: {error}"));
        error.into()
    })
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
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
            retry_backend,
            open_external_url,
            pick_import_directory,
            publish_desktop_import
        ])
        .build(tauri::generate_context!())
        .expect("failed to build Lyra desktop shell");

    app.run(|app, event| {
        if let tauri::RunEvent::Exit = event {
            let state = app.state::<AppState>();
            state.shutdown();
        }
    });
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
    let launch_result = (|| {
        write_bootstrap_request(&mut child, &bootstrap)?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| LaunchError::invalid_readiness("stdout pipe was not available"))?;
        let stderr = child.stderr.take().ok_or(LaunchError::InvalidReadiness {
            reason: "stderr pipe was not available".to_string(),
            diagnostics: None,
        })?;
        let ready = await_readiness(stdout, stderr, &bootstrap)?;
        Ok::<SidecarReady, LaunchError>(ready)
    })();
    let ready = match launch_result {
        Ok(ready) => ready,
        Err(error) => {
            stop_child(&mut child, None, Some(&sidecar));
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
    stderr: ChildStderr,
    expected: &SidecarBootstrapRequest,
) -> Result<SidecarReady, LaunchError> {
    let (tx, rx) = mpsc::channel();
    let stderr_tail = Arc::new(Mutex::new(StderrTail::default()));
    let stderr_reader = Arc::clone(&stderr_tail);
    let expected_for_thread = expected.clone();
    let expected_secret = expected.session_secret.clone();

    std::thread::spawn(move || {
        let outcome = read_readiness(stdout)
            .and_then(|ready| validate_readiness(ready, &expected_for_thread));
        let _ = tx.send(outcome);
    });

    std::thread::spawn(move || drain_stderr(stderr, stderr_reader));

    match rx.recv_timeout(READY_TIMEOUT) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => {
            Err(LaunchError::ReadinessTimeout { diagnostics: None }
                .with_diagnostics(snapshot_diagnostics(&stderr_tail, &expected_secret)))
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(LaunchError::invalid_readiness(
            "readiness channel closed unexpectedly",
        )
        .with_diagnostics(snapshot_diagnostics(&stderr_tail, &expected_secret))),
    }
    .map_err(|error| error.with_diagnostics(snapshot_diagnostics(&stderr_tail, &expected_secret)))
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

fn drain_stderr(stderr: ChildStderr, tail: Arc<Mutex<StderrTail>>) {
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
    let collapsed = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.is_empty() {
        return None;
    }
    let home = std::env::var("HOME").unwrap_or_default();
    let mut safe = collapsed;
    if !secret.is_empty() {
        safe = safe.replace(secret, "<secret>");
    }
    if !home.is_empty() {
        safe = safe.replace(&home, "<home>");
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
    let status = Command::new(sidecar_path)
        .arg("--publish-desktop-import")
        .env("LYRA_PACKAGED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|_| LaunchError::Import("the staged data could not be published"))?;
    if status.success() {
        Ok(())
    } else {
        Err(LaunchError::Import(
            "the staged data could not be published; the prior data was preserved",
        ))
    }
}

fn child_is_running(child: &mut Child) -> Result<bool, LaunchError> {
    Ok(child.try_wait()?.is_none())
}

fn stop_backend(backend: &mut ManagedBackend) {
    stop_child(
        &mut backend.child,
        Some(&backend.bootstrap),
        Some(&backend.sidecar_path),
    );
}

fn stop_child(
    child: &mut Child,
    bootstrap: Option<&BootstrapPayload>,
    sidecar_path: Option<&Path>,
) {
    if child.try_wait().ok().flatten().is_some() {
        reclaim_helpers(sidecar_path);
        return;
    }
    if let Some(bootstrap) = bootstrap {
        if request_graceful_shutdown(bootstrap).is_ok()
            && wait_for_exit(child, SHUTDOWN_GRACE_TIMEOUT)
        {
            reclaim_helpers(sidecar_path);
            return;
        }
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    {
        if wait_for_exit(child, SHUTDOWN_GRACE_TIMEOUT) {
            reclaim_helpers(sidecar_path);
            return;
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    reclaim_helpers(sidecar_path);
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

fn reclaim_helpers(sidecar_path: Option<&Path>) {
    let Some(sidecar_path) = sidecar_path else {
        return;
    };
    let mut sidecar_command = Command::new(sidecar_path);
    sidecar_command
        .arg("--reclaim-helpers")
        .env("LYRA_PACKAGED", "1");
    if run_reclaim_command(&mut sidecar_command, "").is_ok() {
        return;
    }
    if !looks_like_dev_sidecar(sidecar_path) {
        return;
    }
    let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf);
    let Some(repo_root) = repo_root else {
        return;
    };
    let mut command = Command::new("python3");
    command
        .arg("-m")
        .arg("backend.llm.helper_reclaim")
        .current_dir(repo_root);
    let _ = run_reclaim_command(&mut command, "");
}

fn run_reclaim_command(command: &mut Command, secret: &str) -> Result<(), ()> {
    let output = command.output().map_err(|_| ())?;
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
    log_event(&format!("helper reclaim failed: {detail}"));
    Err(())
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
            return Err(LaunchError::Io(std::io::Error::last_os_error()));
        }
        let clear_close_on_exec = previous_flags & !libc::FD_CLOEXEC;
        let set_result = unsafe { libc::fcntl(fd, libc::F_SETFD, clear_close_on_exec) };
        if set_result < 0 {
            return Err(LaunchError::Io(std::io::Error::last_os_error()));
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
        std::env::set_var("HOME", "/Users/private");
        let detail = sanitize_diagnostics(
            "prefix /Users/private/Library/Logs aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa café",
            &"a".repeat(64),
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
}
