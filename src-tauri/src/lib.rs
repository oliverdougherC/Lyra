use serde::{Deserialize, Serialize};
use std::fmt;
use std::fs::File;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, State};

const SESSION_HEADER_NAME: &str = "X-Lyra-Session";
const SIDECAR_NAME: &str = "lyra-backend";
const MAX_READY_LINE_BYTES: usize = 4096;
const READY_TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
struct BootstrapPayload {
    api_base: String,
    session_header: Option<String>,
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

#[derive(Debug, Serialize)]
#[serde(rename_all = "snake_case")]
struct SidecarBootstrapRequest {
    handshake_version: u8,
    socket_fd: i32,
    parent_pid: u32,
    listener_addr: String,
    session_header_name: &'static str,
    session_secret: String,
}

#[derive(Debug, Deserialize, PartialEq, Eq)]
struct SidecarReady {
    api_base: String,
    #[serde(default)]
    session_header: Option<String>,
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
    bootstrap: BootstrapPayload,
}

#[derive(Debug)]
enum LaunchError {
    Io(std::io::Error),
    Json(serde_json::Error),
    MissingSidecar { expected: Vec<String> },
    ReadinessTimeout,
    InvalidReadiness(&'static str),
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
            Self::ReadinessTimeout => {
                write!(f, "lyra-backend did not report readiness within 20 seconds")
            }
            Self::InvalidReadiness(reason) => {
                write!(f, "lyra-backend reported invalid readiness: {reason}")
            }
            Self::Poisoned => write!(f, "desktop shell state is unavailable after a prior panic"),
        }
    }
}

impl std::error::Error for LaunchError {}

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

impl AppState {
    fn ensure_backend(
        &self,
        app: &AppHandle,
        force_restart: bool,
    ) -> Result<BootstrapPayload, LaunchError> {
        let mut lifecycle = self.lifecycle.lock().map_err(|_| LaunchError::Poisoned)?;

        if !force_restart {
            if let Some(existing) = lifecycle.backend.as_mut() {
                if child_is_running(&mut existing.child)? {
                    return Ok(existing.bootstrap.clone());
                }
                log_event("backend exited; preparing a restart");
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

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            focus_main_window(app);
        }))
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![desktop_bootstrap, retry_backend])
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
        handshake_version: 1,
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
        let stdout = child.stdout.take().ok_or(LaunchError::InvalidReadiness(
            "stdout pipe was not available",
        ))?;
        let stderr = child.stderr.take().ok_or(LaunchError::InvalidReadiness(
            "stderr pipe was not available",
        ))?;
        let ready = await_readiness(stdout, stderr)?;
        validate_api_base(&ready.api_base)?;
        Ok::<SidecarReady, LaunchError>(ready)
    })();
    let ready = match launch_result {
        Ok(ready) => ready,
        Err(error) => {
            stop_child(&mut child);
            return Err(error);
        }
    };

    log_event("backend reported readiness");
    Ok(ManagedBackend {
        child,
        bootstrap: BootstrapPayload {
            api_base: ready.api_base,
            session_header: Some(ready.session_header.unwrap_or(session_secret)),
        },
    })
}

fn write_bootstrap_request(
    child: &mut Child,
    bootstrap: &SidecarBootstrapRequest,
) -> Result<(), LaunchError> {
    let mut stdin = child.stdin.take().ok_or(LaunchError::InvalidReadiness(
        "stdin pipe was not available",
    ))?;
    serde_json::to_writer(&mut stdin, bootstrap)?;
    stdin.write_all(b"\n")?;
    stdin.flush()?;
    drop(stdin);
    Ok(())
}

fn await_readiness(stdout: ChildStdout, stderr: ChildStderr) -> Result<SidecarReady, LaunchError> {
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let outcome = read_readiness(stdout);
        let _ = tx.send(outcome);
    });

    std::thread::spawn(move || drain_and_discard(stderr));

    match rx.recv_timeout(READY_TIMEOUT) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => Err(LaunchError::ReadinessTimeout),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(LaunchError::InvalidReadiness(
            "readiness channel closed unexpectedly",
        )),
    }
}

fn read_readiness(stdout: ChildStdout) -> Result<SidecarReady, LaunchError> {
    let mut reader = BufReader::new(stdout);
    let mut line = Vec::with_capacity(256);
    let read = reader.read_until(b'\n', &mut line)?;
    if read == 0 {
        return Err(LaunchError::InvalidReadiness(
            "stdout closed before a readiness line arrived",
        ));
    }
    if line.len() > MAX_READY_LINE_BYTES {
        return Err(LaunchError::InvalidReadiness(
            "readiness line exceeded the 4 KiB limit",
        ));
    }

    while line
        .last()
        .is_some_and(|byte| matches!(byte, b'\n' | b'\r'))
    {
        line.pop();
    }

    let ready: SidecarReady = serde_json::from_slice(&line)?;
    std::thread::spawn(move || drain_and_discard(reader));
    Ok(ready)
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

fn validate_api_base(api_base: &str) -> Result<(), LaunchError> {
    if api_base.starts_with("http://127.0.0.1:") {
        return Ok(());
    }
    Err(LaunchError::InvalidReadiness(
        "api_base must stay on 127.0.0.1 with an explicit port",
    ))
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

fn child_is_running(child: &mut Child) -> Result<bool, LaunchError> {
    Ok(child.try_wait()?.is_none())
}

fn stop_backend(backend: &mut ManagedBackend) {
    stop_child(&mut backend.child);
}

fn stop_child(child: &mut Child) {
    if child.try_wait().ok().flatten().is_some() {
        return;
    }
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as i32, libc::SIGTERM);
    }
    #[cfg(unix)]
    {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            match child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn focus_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn log_event(message: &str) {
    let trimmed = if message.len() > 160 {
        &message[..160]
    } else {
        message
    };
    eprintln!("[lyra-desktop] {trimmed}");
}

#[cfg(unix)]
use std::os::fd::AsRawFd;

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

    #[test]
    fn parses_readiness_payload() {
        let ready: SidecarReady =
            serde_json::from_str(r#"{"api_base":"http://127.0.0.1:43123"}"#).unwrap();

        assert_eq!(
            ready,
            SidecarReady {
                api_base: "http://127.0.0.1:43123".to_string(),
                session_header: None,
            }
        );
    }

    #[test]
    fn rejects_non_loopback_api_base() {
        let error = validate_api_base("http://0.0.0.0:8000").unwrap_err();

        assert!(error
            .to_string()
            .contains("api_base must stay on 127.0.0.1"));
    }

    #[test]
    fn formats_target_specific_sidecar_name() {
        let filename = sidecar_filename();

        assert!(filename.starts_with("lyra-backend-"));
        assert!(!filename.contains(' '));
    }
}
