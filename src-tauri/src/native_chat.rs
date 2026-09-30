//! macOS chat transcript displayed by a WKWebView inside an AppKit scroll view.
//! The main webview remains the owner of conversations, streaming, and navigation.

use serde::Deserialize;
#[cfg(target_os = "macos")]
use std::ffi::CStr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, Webview};

#[derive(Default)]
pub struct NativeChatState {
    inner: Arc<Mutex<Option<NativeChatHandle>>>,
    next_instance: AtomicU64,
}

#[cfg(target_os = "macos")]
static CHAT_APP: std::sync::OnceLock<AppHandle> = std::sync::OnceLock::new();

#[cfg(target_os = "macos")]
#[no_mangle]
pub unsafe extern "C" fn lyra_native_chat_scroll_changed(
    host_id: *const std::ffi::c_char,
    at_bottom: bool,
    ratio: f64,
) {
    if host_id.is_null() || !ratio.is_finite() {
        return;
    }
    let Ok(host_id) = (unsafe { CStr::from_ptr(host_id) }).to_str() else {
        return;
    };
    if let Some(app) = CHAT_APP.get() {
        if let Some(main) = app.get_webview("main") {
            let Ok(host_id) = serde_json::to_string(host_id) else {
                return;
            };
            let _ = main.eval(format!(
                "window.dispatchEvent(new CustomEvent('lyra:native-scroll-state', {{ detail: {{ hostId: {host_id}, atBottom: {at_bottom}, ratio: {ratio} }} }}))"
            ));
        }
    }
}

struct NativeChatHandle {
    owner: Arc<NativeChatOwner>,
    host_id: String,
    instance_id: u64,
    webviews: Vec<Webview>,
    loaded: Vec<bool>,
    ready: Vec<bool>,
    sent_versions: Vec<u64>,
    section_revisions: Vec<Arc<AtomicU64>>,
    last_sections: Vec<serde_json::Value>,
    scope: String,
    version: u64,
    #[cfg(target_os = "macos")]
    scroll: usize,
}

// Main-thread closures capture this token, never a bare scroll pointer. Teardown
// revokes it before scheduling the only operation that releases the Objective-C view.
struct NativeChatOwner {
    host_id: String,
    generation: AtomicU64,
    geometry: AtomicU64,
    scroll_order: AtomicU64,
    presentation: AtomicU64,
    client_presentation: AtomicU64,
    live: AtomicBool,
}

impl NativeChatOwner {
    fn new(host_id: String, generation: u64) -> Self {
        Self {
            host_id,
            generation: AtomicU64::new(generation),
            geometry: AtomicU64::new(0),
            scroll_order: AtomicU64::new(0),
            presentation: AtomicU64::new(0),
            client_presentation: AtomicU64::new(0),
            live: AtomicBool::new(true),
        }
    }

    fn matches_host(&self, host_id: &str) -> bool {
        self.live.load(Ordering::Acquire) && self.host_id == host_id
    }

    fn may_run(&self, generation: u64) -> bool {
        self.matches_host(&self.host_id) && self.generation.load(Ordering::Acquire) == generation
    }

    fn set_generation(&self, generation: u64) {
        self.generation.store(generation, Ordering::Release);
    }

    fn next_geometry(&self) -> u64 {
        self.geometry.fetch_add(1, Ordering::AcqRel) + 1
    }

    fn may_set_geometry(&self, sequence: u64) -> bool {
        self.matches_host(&self.host_id) && self.geometry.load(Ordering::Acquire) == sequence
    }

    fn next_scroll(&self) -> u64 {
        self.scroll_order.fetch_add(1, Ordering::AcqRel) + 1
    }

    fn may_scroll(&self, sequence: u64) -> bool {
        self.matches_host(&self.host_id) && self.scroll_order.load(Ordering::Acquire) == sequence
    }

    fn next_presentation(&self) -> u64 {
        self.presentation.fetch_add(1, Ordering::AcqRel) + 1
    }

    fn may_present(&self, sequence: u64) -> bool {
        self.matches_host(&self.host_id) && self.presentation.load(Ordering::Acquire) == sequence
    }

    fn latest_client_presentation(&self) -> u64 {
        self.client_presentation.load(Ordering::Acquire)
    }

    fn may_present_native(&self, native_sequence: u64, client_presentation: u64) -> bool {
        self.may_present(native_sequence)
            && self.latest_client_presentation() == client_presentation
    }

    fn accept_presentation_request(&self, request_id: u64) -> Option<u64> {
        if request_id == 0 || !self.matches_host(&self.host_id) {
            return None;
        }
        self.client_presentation
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                (request_id > current).then_some(request_id)
            })
            .ok()
            .map(|_| self.presentation.load(Ordering::Acquire))
    }

    fn may_present_request(&self, request_id: u64, native_sequence: u64) -> bool {
        self.may_present(native_sequence)
            && self.client_presentation.load(Ordering::Acquire) == request_id
    }

    fn deactivate(&self) {
        self.live.store(false, Ordering::Release);
    }
}

fn can_show(
    current_scope: &str,
    current_version: u64,
    scope: &str,
    version: u64,
    ready: &[bool],
) -> bool {
    current_scope == scope && current_version == version && ready.iter().all(|value| *value)
}

fn may_size_section(owner: &NativeChatOwner, revision: &AtomicU64, version: u64) -> bool {
    owner.matches_host(&owner.host_id) && revision.load(Ordering::Acquire) == version
}

fn acknowledge_section(
    ready: &mut [bool],
    sent_versions: &[u64],
    index: usize,
    version: u64,
    current_version: u64,
) -> Option<u64> {
    if sent_versions.get(index) != Some(&version) || version > current_version {
        return None;
    }
    ready[index] = true;
    ready.iter().all(|value| *value).then_some(current_version)
}

fn current_turn_callback(
    kind: &str,
    action: &serde_json::Value,
    row: &serde_json::Value,
    live_generation: Option<&serde_json::Value>,
) -> bool {
    let generation = action.get("generation").and_then(serde_json::Value::as_str);
    let epoch = row.get("contentEpoch").and_then(serde_json::Value::as_u64);
    let revision = row
        .get("contentRevision")
        .and_then(serde_json::Value::as_u64);
    row.get("streaming").and_then(serde_json::Value::as_bool) == Some(true)
        && generation.is_some()
        && row.get("generation") == action.get("generation")
        && action.get("generation") == live_generation
        && epoch.is_some()
        && epoch
            == action
                .get("contentEpoch")
                .and_then(serde_json::Value::as_u64)
        && (kind != "reveal-complete"
            || (revision.is_some()
                && revision
                    == action
                        .get("contentRevision")
                        .and_then(serde_json::Value::as_u64)))
}

fn current_action_version(
    sent_version: u64,
    current_version: u64,
    action_version: u64,
    delayed_turn_event: bool,
) -> bool {
    action_version > 0
        && action_version <= current_version
        && if delayed_turn_event {
            action_version <= sent_version
        } else {
            action_version == sent_version
        }
}

fn valid_host_id(host_id: &str) -> bool {
    (8..=128).contains(&host_id.len())
        && host_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ChatRect {
    x: f64,
    top: f64,
    width: f64,
    height: f64,
}

impl ChatRect {
    fn valid(self) -> bool {
        [self.x, self.top, self.width, self.height]
            .into_iter()
            .all(f64::is_finite)
            && self.x >= 0.0
            && self.top >= 0.0
            && (1.0..=10_000.0).contains(&self.width)
            && (1.0..=10_000.0).contains(&self.height)
    }
}

fn caller_is(webview: &Webview, label: &str) -> Result<(), String> {
    if webview.label() == label {
        Ok(())
    } else {
        Err("This chat operation is unavailable from this view.".into())
    }
}

fn section_index(webview: &Webview) -> Result<usize, String> {
    webview
        .label()
        .strip_prefix("native-chat-")
        .and_then(|value| value.rsplit('-').next())
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|index| *index < 64)
        .ok_or_else(|| "This chat operation is unavailable from this view.".into())
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    use std::ffi::c_void;
    use std::sync::mpsc;
    use std::time::Duration;
    use tauri::webview::{NewWindowResponse, WebviewBuilder};
    use tauri::{LogicalPosition, LogicalSize, WebviewUrl};

    unsafe extern "C" {
        fn lyra_native_chat_attach(
            window: *mut c_void,
            webview: *mut c_void,
            x: f64,
            top: f64,
            width: f64,
            height: f64,
            host_id: *const std::ffi::c_char,
        ) -> *mut c_void;
        fn lyra_native_chat_set_frame(
            scroll: *mut c_void,
            x: f64,
            top: f64,
            width: f64,
            height: f64,
        );
        fn lyra_native_chat_set_section_height(scroll: *mut c_void, index: usize, height: f64);
        fn lyra_native_chat_add_section(scroll: *mut c_void, webview: *mut c_void) -> bool;
        fn lyra_native_chat_remove_last_section(scroll: *mut c_void);
        fn lyra_native_chat_scroll_to_bottom(scroll: *mut c_void);
        fn lyra_native_chat_set_scroll_ratio(scroll: *mut c_void, ratio: f64);
        fn lyra_native_chat_set_visible(scroll: *mut c_void, visible: bool);
        fn lyra_native_chat_detach(scroll: *mut c_void);
    }

    fn handle(
        app: &AppHandle,
        host_id: &str,
    ) -> Result<(usize, Arc<NativeChatOwner>, u64), String> {
        let state = app.state::<NativeChatState>();
        let _ = CHAT_APP.set(app.clone());
        let guard = state
            .inner
            .lock()
            .map_err(|_| "Chat scroll state is unavailable")?;
        let current = guard.as_ref().ok_or("The native chat view is not open")?;
        if !current.owner.matches_host(host_id) {
            return Err("The native chat host is no longer current.".into());
        }
        Ok((current.scroll, Arc::clone(&current.owner), current.version))
    }

    async fn set_visibility(
        app: AppHandle,
        scroll: usize,
        owner: Arc<NativeChatOwner>,
        request_id: u64,
        native_sequence: u64,
        version: Option<u64>,
        visible: bool,
    ) -> Result<(), String> {
        let (tx, rx) = mpsc::channel();
        app.run_on_main_thread(move || {
            let current = owner.may_present_request(request_id, native_sequence)
                && version.map_or(true, |version| owner.may_run(version));
            if current {
                unsafe { lyra_native_chat_set_visible(scroll as *mut c_void, visible) };
            }
            let _ = tx.send(current);
        })
        .map_err(|_| "The chat visibility could not be changed")?;
        let applied =
            tauri::async_runtime::spawn_blocking(move || rx.recv_timeout(Duration::from_secs(5)))
                .await
                .map_err(|_| "The chat visibility stopped unexpectedly")?
                .map_err(|_| "The chat visibility did not complete")?;
        if applied {
            Ok(())
        } else {
            Err("The native chat owner is no longer current.".into())
        }
    }

    #[tauri::command]
    pub async fn native_chat_mount(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        rect: ChatRect,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let _ = CHAT_APP.set(app.clone());
        if !rect.valid() {
            return Err("The chat viewport has invalid dimensions.".into());
        }
        if !valid_host_id(&host_id) {
            return Err("The native chat host is invalid.".into());
        }
        let state = app.state::<NativeChatState>();
        let inner = Arc::clone(&state.inner);
        let instance_id = state.next_instance.fetch_add(1, Ordering::Relaxed);
        tauri::async_runtime::spawn_blocking(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            if let Some(existing) = guard.as_ref() {
                if existing.host_id != host_id {
                    return Err("Another native chat host is still mounted".into());
                }
                let scroll = existing.scroll;
                let owner = Arc::clone(&existing.owner);
                let geometry = owner.next_geometry();
                app.run_on_main_thread(move || unsafe {
                    if owner.may_set_geometry(geometry) {
                        lyra_native_chat_set_frame(
                            scroll as *mut c_void,
                            rect.x,
                            rect.top,
                            rect.width,
                            rect.height,
                        );
                    }
                })
                .map_err(|_| "The chat viewport could not be positioned")?;
                return Ok(());
            }

            let window = app
                .get_window("main")
                .ok_or("The desktop window is unavailable")?;
            let builder = WebviewBuilder::new(
                format!("native-chat-{instance_id}-0"),
                WebviewUrl::App(format!("native-transcript.html?hostId={host_id}").into()),
            )
            .on_navigation(|url| {
                url.scheme() == "tauri"
                    && url.host_str() == Some("localhost")
                    && url.path() == "/native-transcript.html"
            })
            .on_new_window(|_, _| NewWindowResponse::Deny);
            let child = window
                .add_child(
                    builder,
                    LogicalPosition::new(rect.x, rect.top),
                    LogicalSize::new(rect.width, rect.height),
                )
                .map_err(|_| "The native chat view could not be created")?;
            let (tx, rx) = mpsc::channel();
            let host_id_c = std::ffi::CString::new(host_id.as_str())
                .map_err(|_| "The native chat host is invalid")?;
            child
                .with_webview(move |platform| {
                    let scroll = unsafe {
                        lyra_native_chat_attach(
                            platform.ns_window(),
                            platform.inner(),
                            rect.x,
                            rect.top,
                            rect.width,
                            rect.height,
                            host_id_c.as_ptr(),
                        )
                    };
                    if tx.send(scroll as usize).is_err() && !scroll.is_null() {
                        // The blocking mount timed out. This callback still owns the retained
                        // view and is already on the main thread, so release it here.
                        unsafe { lyra_native_chat_detach(scroll) };
                    }
                })
                .map_err(|_| "The native chat scroll view could not be attached")?;
            let scroll = rx
                .recv_timeout(Duration::from_secs(5))
                .map_err(|_| "The native chat scroll view did not become ready")?;
            if scroll == 0 {
                let _ = child.close();
                return Err("The native chat scroll view could not be attached".into());
            }
            *guard = Some(NativeChatHandle {
                owner: Arc::new(NativeChatOwner::new(host_id.clone(), 0)),
                host_id,
                instance_id,
                webviews: vec![child],
                loaded: vec![false],
                ready: vec![false],
                sent_versions: vec![0],
                section_revisions: vec![Arc::new(AtomicU64::new(0))],
                last_sections: vec![serde_json::Value::Null],
                scope: String::new(),
                version: 0,
                scroll,
            });
            Ok(())
        })
        .await
        .map_err(|_| "The native chat view stopped unexpectedly".to_string())?
    }

    #[tauri::command]
    pub async fn native_chat_set_frame(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        rect: ChatRect,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        if !rect.valid() {
            return Err("The chat viewport has invalid dimensions.".into());
        }
        let (scroll, owner, _) = handle(&app, &host_id)?;
        let geometry = owner.next_geometry();
        app.run_on_main_thread(move || unsafe {
            if owner.may_set_geometry(geometry) {
                lyra_native_chat_set_frame(
                    scroll as *mut c_void,
                    rect.x,
                    rect.top,
                    rect.width,
                    rect.height,
                );
            }
        })
        .map_err(|_| "The chat viewport could not be positioned".into())
    }

    #[tauri::command]
    pub async fn native_chat_set_content_height(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        scope: String,
        version: u64,
        height: f64,
    ) -> Result<(), String> {
        let index = section_index(&webview)?;
        if !height.is_finite() || !(1.0..=18_000.0).contains(&height) {
            return Err("The chat content has invalid dimensions.".into());
        }
        let (scroll, owner, revision) = {
            let state = app.state::<NativeChatState>();
            let guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_ref().ok_or("The native chat view is not open")?;
            if !current.owner.matches_host(&host_id)
                || current.scope != scope
                || current.sent_versions.get(index) != Some(&version)
            {
                return Err("The chat section is no longer current.".into());
            }
            if current.webviews.get(index).map(Webview::label) != Some(webview.label()) {
                return Err("The chat section is no longer open.".into());
            }
            (
                current.scroll,
                Arc::clone(&current.owner),
                Arc::clone(&current.section_revisions[index]),
            )
        };
        let (tx, rx) = mpsc::channel();
        app.run_on_main_thread(move || {
            let current = may_size_section(&owner, &revision, version);
            if current {
                unsafe {
                    lyra_native_chat_set_section_height(scroll as *mut c_void, index, height)
                };
            }
            let _ = tx.send(current);
        })
        .map_err(|_| "The chat content could not be sized")?;
        let applied =
            tauri::async_runtime::spawn_blocking(move || rx.recv_timeout(Duration::from_secs(5)))
                .await
                .map_err(|_| "The chat sizing stopped unexpectedly")?
                .map_err(|_| "The chat sizing did not complete")?;
        if applied {
            Ok(())
        } else {
            Err("The chat section is no longer current.".into())
        }
    }

    #[tauri::command]
    pub async fn native_chat_render(
        app: AppHandle,
        webview: Webview,
        snapshot: serde_json::Value,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let rows = snapshot
            .get("rows")
            .and_then(serde_json::Value::as_array)
            .ok_or("The chat update has no rows")?
            .clone();
        if rows.len() > 2048 {
            return Err("The chat contains too many rows for the native view.".into());
        }
        let version = snapshot
            .get("version")
            .and_then(serde_json::Value::as_u64)
            .ok_or("The chat update has no version")?;
        let scope = snapshot
            .get("scope")
            .and_then(serde_json::Value::as_str)
            .ok_or("The chat update has no scope")?
            .to_owned();
        let host_id = snapshot
            .get("hostId")
            .and_then(serde_json::Value::as_str)
            .ok_or("The chat update has no host")?
            .to_owned();
        let count = rows.len().div_ceil(32).max(1);
        if count > 64 {
            return Err("The chat contains too many sections for the native view.".into());
        }
        if serde_json::to_vec(&snapshot)
            .map_err(|_| "The chat update is invalid")?
            .len()
            > 8_000_000
        {
            return Err("The chat update is too large for the native view.".into());
        }
        let state = app.state::<NativeChatState>();
        let inner = Arc::clone(&state.inner);
        tauri::async_runtime::spawn_blocking(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_mut().ok_or("The native chat view is not open")?;
            if !current.owner.matches_host(&host_id) {
                return Err("The native chat host is no longer current.".into());
            }
            if version <= current.version {
                return Ok(());
            }
            current.version = version;
            current.owner.set_generation(version);
            if scope != current.scope {
                current.scope = scope;
                current.ready.fill(false);
                for revision in &current.section_revisions {
                    revision.store(0, Ordering::Release);
                }
                current.last_sections.fill(serde_json::Value::Null);
                let scroll = current.scroll;
                let owner = Arc::clone(&current.owner);
                let client_presentation = owner.latest_client_presentation();
                let presentation = owner.next_presentation();
                app.run_on_main_thread(move || unsafe {
                    if owner.may_present_native(presentation, client_presentation) {
                        lyra_native_chat_set_visible(scroll as *mut c_void, false);
                    }
                })
                .map_err(|_| "The previous chat could not be hidden")?;
            }
            let window = app
                .get_window("main")
                .ok_or("The desktop window is unavailable")?;
            while current.webviews.len() < count {
                let index = current.webviews.len();
                let label = format!("native-chat-{}-{index}", current.instance_id);
                let builder = WebviewBuilder::new(
                    label,
                    WebviewUrl::App(format!("native-transcript.html?hostId={host_id}").into()),
                )
                .on_navigation(|url| {
                    url.scheme() == "tauri"
                        && url.host_str() == Some("localhost")
                        && url.path() == "/native-transcript.html"
                })
                .on_new_window(|_, _| NewWindowResponse::Deny);
                let child = window
                    .add_child(
                        builder,
                        LogicalPosition::new(0.0, 0.0),
                        LogicalSize::new(800.0, 800.0),
                    )
                    .map_err(|_| "A chat section could not be created")?;
                let scroll = current.scroll;
                let owner = Arc::clone(&current.owner);
                let (tx, rx) = mpsc::channel();
                child
                    .with_webview(move |platform| {
                        let result = owner.matches_host(&owner.host_id)
                            && unsafe {
                                lyra_native_chat_add_section(
                                    scroll as *mut c_void,
                                    platform.inner(),
                                )
                            };
                        let _ = tx.send(result);
                    })
                    .map_err(|_| "A chat section could not be attached")?;
                if !rx
                    .recv_timeout(Duration::from_secs(5))
                    .map_err(|_| "A chat section did not become ready")?
                {
                    let _ = child.close();
                    return Err("A chat section could not be attached".into());
                }
                current.webviews.push(child);
                current.loaded.push(false);
                current.ready.push(false);
                current.sent_versions.push(0);
                current.section_revisions.push(Arc::new(AtomicU64::new(0)));
                current.last_sections.push(serde_json::Value::Null);
            }
            while current.webviews.len() > count {
                let child = current.webviews.pop().expect("section exists");
                current.loaded.pop();
                current.ready.pop();
                current.sent_versions.pop();
                current
                    .section_revisions
                    .pop()
                    .expect("section exists")
                    .store(0, Ordering::Release);
                current.last_sections.pop();
                let scroll = current.scroll;
                let owner = Arc::clone(&current.owner);
                let (tx, rx) = mpsc::channel();
                app.run_on_main_thread(move || {
                    if owner.matches_host(&owner.host_id) {
                        unsafe { lyra_native_chat_remove_last_section(scroll as *mut c_void) };
                    }
                    let _ = child.close();
                    let _ = tx.send(());
                })
                .map_err(|_| "A chat section could not close")?;
                rx.recv_timeout(Duration::from_secs(5))
                    .map_err(|_| "A chat section did not close")?;
            }
            let mut base = snapshot.clone();
            base["rows"] = serde_json::Value::Null;
            for index in 0..current.webviews.len() {
                if !current.loaded[index] {
                    continue;
                }
                let mut section = base.clone();
                section["rows"] = serde_json::Value::Array(
                    rows.iter().skip(index * 32).take(32).cloned().collect(),
                );
                let mut comparison = section.clone();
                comparison["version"] = serde_json::Value::Null;
                if current.last_sections[index] == comparison {
                    continue;
                }
                let payload =
                    serde_json::to_string(&section).map_err(|_| "The chat section is invalid")?;
                current.webviews[index]
                    .eval(format!("window.__lyraNativeChatReceive?.({payload})"))
                    .map_err(|_| "The native chat view could not be updated")?;
                current.ready[index] = false;
                current.sent_versions[index] = version;
                current.section_revisions[index].store(version, Ordering::Release);
                current.last_sections[index] = comparison;
            }
            Ok(())
        })
        .await
        .map_err(|_| "The native chat view stopped unexpectedly".to_string())?
    }

    #[tauri::command]
    pub async fn native_chat_action(
        app: AppHandle,
        webview: Webview,
        action: serde_json::Value,
    ) -> Result<(), String> {
        let index = section_index(&webview)?;
        let kind = action
            .get("kind")
            .and_then(serde_json::Value::as_str)
            .ok_or("The chat action is invalid")?;
        if !matches!(
            kind,
            "ready"
                | "content-ready"
                | "overflow"
                | "retry"
                | "reveal-complete"
                | "reasoning-open"
                | "navigate"
                | "selection"
        ) {
            return Err("The chat action is unavailable.".into());
        }
        let payload = serde_json::to_string(&action).map_err(|_| "The chat action is invalid")?;
        if payload.len() > 4096 {
            return Err("The chat action is too large.".into());
        }
        let host_id = action
            .get("hostId")
            .and_then(serde_json::Value::as_str)
            .ok_or("The chat action has no host")?;
        let mut forwarded = action.clone();
        {
            let state = app.state::<NativeChatState>();
            let mut guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_mut().ok_or("The native chat view is not open")?;
            if !current.owner.matches_host(host_id)
                || current.webviews.get(index).map(Webview::label) != Some(webview.label())
            {
                return Ok(());
            }
            if kind == "ready" {
                current.loaded[index] = true;
                current.ready[index] = false;
                current.sent_versions[index] = 0;
                current.section_revisions[index].store(0, Ordering::Release);
                current.last_sections[index] = serde_json::Value::Null;
                let scroll = current.scroll;
                let owner = Arc::clone(&current.owner);
                let client_presentation = owner.latest_client_presentation();
                let presentation = owner.next_presentation();
                app.run_on_main_thread(move || unsafe {
                    if owner.may_present_native(presentation, client_presentation) {
                        lyra_native_chat_set_visible(scroll as *mut c_void, false);
                    }
                })
                .map_err(|_| "The chat view could not be hidden")?;
            } else {
                let version = action.get("version").and_then(serde_json::Value::as_u64);
                let sent_version = current.sent_versions.get(index).copied().unwrap_or(0);
                let delayed_turn_event = matches!(kind, "reveal-complete" | "reasoning-open");
                if action.get("scope").and_then(serde_json::Value::as_str)
                    != Some(current.scope.as_str())
                    || !version.is_some_and(|value| {
                        current_action_version(
                            sent_version,
                            current.version,
                            value,
                            delayed_turn_event,
                        )
                    })
                {
                    return Ok(());
                }
                if matches!(
                    kind,
                    "retry" | "selection" | "reveal-complete" | "reasoning-open" | "navigate"
                ) && !(kind == "selection"
                    && action.get("rowKey").is_some_and(serde_json::Value::is_null))
                {
                    let row_key = action.get("rowKey").and_then(serde_json::Value::as_str);
                    let generation = action.get("generation");
                    let row = current.last_sections[index]
                        .get("rows")
                        .and_then(serde_json::Value::as_array)
                        .and_then(|rows| {
                            rows.iter().find(|row| {
                                row.get("key").and_then(serde_json::Value::as_str) == row_key
                            })
                        });
                    let Some(row) = row else {
                        return Ok(());
                    };
                    if kind == "selection" && row.get("generation") != generation {
                        return Ok(());
                    }
                    if kind == "retry" && row.get("retryAction") != action.get("action") {
                        return Ok(());
                    }
                    if delayed_turn_event
                        && !current_turn_callback(
                            kind,
                            &action,
                            row,
                            current.last_sections[index].get("liveGeneration"),
                        )
                    {
                        return Ok(());
                    }
                }
                if kind == "content-ready" {
                    let Some(current_version) = acknowledge_section(
                        &mut current.ready,
                        &current.sent_versions,
                        index,
                        version.expect("checked above"),
                        current.version,
                    ) else {
                        return Ok(());
                    };
                    forwarded["version"] = serde_json::Value::from(current_version);
                }
            }
        }
        let main = app
            .get_webview("main")
            .ok_or("The desktop view is unavailable")?;
        let payload =
            serde_json::to_string(&forwarded).map_err(|_| "The chat action is invalid")?;
        main.eval(format!(
            "window.dispatchEvent(new CustomEvent('lyra:native-chat-action', {{ detail: {payload} }}))"
        ))
        .map_err(|_| "The chat action could not be delivered".into())
    }

    #[tauri::command]
    pub async fn native_chat_scroll_to_bottom(
        app: AppHandle,
        webview: Webview,
        host_id: String,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let (scroll, owner, _) = handle(&app, &host_id)?;
        let sequence = owner.next_scroll();
        app.run_on_main_thread(move || unsafe {
            if owner.may_scroll(sequence) {
                lyra_native_chat_scroll_to_bottom(scroll as *mut c_void);
            }
        })
        .map_err(|_| "The chat could not be scrolled".into())
    }

    #[tauri::command]
    pub async fn native_chat_set_scroll_ratio(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        ratio: f64,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        if !ratio.is_finite() || !(0.0..=1.0).contains(&ratio) {
            return Err("The chat scroll ratio is invalid.".into());
        }
        let (scroll, owner, _) = handle(&app, &host_id)?;
        let sequence = owner.next_scroll();
        let (tx, rx) = mpsc::channel();
        app.run_on_main_thread(move || {
            let current = owner.may_scroll(sequence);
            if current {
                unsafe { lyra_native_chat_set_scroll_ratio(scroll as *mut c_void, ratio) };
            }
            let _ = tx.send(current);
        })
        .map_err(|_| "The chat could not be scrolled")?;
        let applied =
            tauri::async_runtime::spawn_blocking(move || rx.recv_timeout(Duration::from_secs(5)))
                .await
                .map_err(|_| "The chat scrolling stopped unexpectedly")?
                .map_err(|_| "The chat scrolling did not complete")?;
        if applied {
            Ok(())
        } else {
            Err("The native chat owner is no longer current.".into())
        }
    }

    #[tauri::command]
    pub async fn native_chat_show(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        scope: String,
        version: u64,
        presentation_id: u64,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let (scroll, owner, native_sequence) = {
            let state = app.state::<NativeChatState>();
            let guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_ref().ok_or("The native chat view is not open")?;
            if !current.owner.matches_host(&host_id)
                || current.scope != scope
                || current.version != version
            {
                return Err("The native chat owner is no longer current.".into());
            }
            if !can_show(
                &current.scope,
                current.version,
                &scope,
                version,
                &current.ready,
            ) {
                return Err("The chat content is not ready.".into());
            }
            let sequence = current
                .owner
                .accept_presentation_request(presentation_id)
                .ok_or("The chat visibility request is stale.")?;
            (current.scroll, Arc::clone(&current.owner), sequence)
        };
        set_visibility(
            app,
            scroll,
            owner,
            presentation_id,
            native_sequence,
            Some(version),
            true,
        )
        .await
    }

    #[tauri::command]
    pub async fn native_chat_hide(
        app: AppHandle,
        webview: Webview,
        host_id: String,
        presentation_id: u64,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let (scroll, owner, _) = handle(&app, &host_id)?;
        let native_sequence = owner
            .accept_presentation_request(presentation_id)
            .ok_or("The chat visibility request is stale.")?;
        set_visibility(
            app,
            scroll,
            owner,
            presentation_id,
            native_sequence,
            None,
            false,
        )
        .await
    }

    #[tauri::command]
    pub async fn native_chat_unmount(
        app: AppHandle,
        webview: Webview,
        host_id: String,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let state = app.state::<NativeChatState>();
        let inner = Arc::clone(&state.inner);
        tauri::async_runtime::spawn_blocking(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            if !guard
                .as_ref()
                .is_some_and(|current| current.host_id == host_id)
            {
                return Ok(());
            }
            let current = guard.take().expect("checked above");
            current.owner.deactivate();
            let (tx, rx) = mpsc::channel();
            app.run_on_main_thread(move || {
                unsafe { lyra_native_chat_detach(current.scroll as *mut c_void) };
                for child in current.webviews {
                    let _ = child.close();
                }
                let _ = tx.send(());
            })
            .map_err(|_| "The native chat view could not close")?;
            rx.recv_timeout(Duration::from_secs(5))
                .map_err(|_| "The native chat view did not close")?;
            Ok(())
        })
        .await
        .map_err(|_| "The native chat view stopped unexpectedly".to_string())?
    }
}

#[cfg(target_os = "macos")]
pub use macos::*;

#[cfg(not(target_os = "macos"))]
mod unsupported {
    use super::*;

    #[tauri::command]
    pub async fn native_chat_mount(
        _: AppHandle,
        _: Webview,
        _: String,
        _: ChatRect,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_set_frame(
        _: AppHandle,
        _: Webview,
        _: String,
        _: ChatRect,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_set_content_height(
        _: AppHandle,
        _: Webview,
        _: String,
        _: String,
        _: u64,
        _: u64,
        _: f64,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub fn native_chat_render(
        _: AppHandle,
        _: Webview,
        _: serde_json::Value,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_action(
        _: AppHandle,
        _: Webview,
        _: serde_json::Value,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_scroll_to_bottom(
        _: AppHandle,
        _: Webview,
        _: String,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_set_scroll_ratio(
        _: AppHandle,
        _: Webview,
        _: String,
        _: f64,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_show(
        _: AppHandle,
        _: Webview,
        _: String,
        _: String,
        _: u64,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_hide(
        _: AppHandle,
        _: Webview,
        _: String,
        _: u64,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_unmount(_: AppHandle, _: Webview, _: String) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
}

#[cfg(not(target_os = "macos"))]
pub use unsupported::*;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stale_host_cannot_control_replacement() {
        let owner = NativeChatOwner::new("new-host".into(), 2);
        assert!(!owner.matches_host("old-host"));
        assert!(owner.matches_host("new-host"));
        owner.deactivate();
        assert!(!owner.matches_host("new-host"));
    }

    #[test]
    fn queued_work_does_not_survive_new_snapshot_or_unmount() {
        let owner = NativeChatOwner::new("host".into(), 2);
        assert!(owner.may_run(2));
        owner.set_generation(3);
        assert!(!owner.may_run(2));
        assert!(owner.may_run(3));
        owner.deactivate();
        assert!(!owner.may_run(3));
    }

    #[test]
    fn later_visibility_request_supersedes_queued_show_or_hide() {
        let owner = NativeChatOwner::new("host".into(), 2);
        let show = owner.next_presentation();
        assert!(owner.may_present(show));
        let hide = owner.next_presentation();
        assert!(!owner.may_present(show));
        assert!(owner.may_present(hide));
        owner.deactivate();
        assert!(!owner.may_present(hide));
    }

    #[test]
    fn older_show_arriving_after_newer_hide_is_rejected() {
        let owner = NativeChatOwner::new("host".into(), 2);
        let hide = owner.accept_presentation_request(2).expect("new hide");
        assert!(owner.may_present_request(2, hide));
        assert!(owner.accept_presentation_request(1).is_none());
        assert!(owner.may_present_request(2, hide));
        let show = owner.accept_presentation_request(3).expect("new show");
        assert!(!owner.may_present_request(2, hide));
        assert!(owner.may_present_request(3, show));
    }

    #[test]
    fn current_client_show_supersedes_queued_native_scope_hide() {
        let owner = NativeChatOwner::new("host".into(), 2);
        let previous_client = owner.latest_client_presentation();
        let scope_hide = owner.next_presentation();
        let show = owner.accept_presentation_request(1).expect("current show");
        assert!(!owner.may_present_native(scope_hide, previous_client));
        assert!(owner.may_present_request(1, show));
    }

    #[test]
    fn show_needs_current_scope_version_and_every_section_ready() {
        assert!(!can_show("a", 4, "b", 4, &[true, true]));
        assert!(!can_show("a", 4, "a", 3, &[true, true]));
        assert!(!can_show("a", 4, "a", 4, &[true, false]));
        assert!(can_show("a", 4, "a", 4, &[true, true]));
    }

    #[test]
    fn unchanged_early_section_can_resize_after_tail_update_and_finish_reordered_readiness() {
        let rows: Vec<_> = (0..65)
            .map(|index| serde_json::json!({ "key": index }))
            .collect();
        let sections: Vec<_> = rows.chunks(32).collect();
        assert_eq!(sections.len(), 3);
        let owner = NativeChatOwner::new("host".into(), 1);
        let revisions: Vec<_> = (0..3).map(|_| AtomicU64::new(1)).collect();
        let mut sent_versions = vec![1; 3];
        let mut ready = vec![false; 3];

        // The tail alone changes. The early section still owns its version 1
        // layout callbacks while the transcript's publication reaches version 2.
        owner.set_generation(2);
        sent_versions[2] = 2;
        revisions[2].store(2, Ordering::Release);
        assert!(may_size_section(&owner, &revisions[0], 1));
        assert_eq!(
            acknowledge_section(&mut ready, &sent_versions, 2, 2, 2),
            None
        );
        assert_eq!(
            acknowledge_section(&mut ready, &sent_versions, 1, 1, 2),
            None
        );
        assert_eq!(
            acknowledge_section(&mut ready, &sent_versions, 0, 1, 2),
            Some(2)
        );
        assert!(can_show("conversation", 2, "conversation", 2, &ready));

        revisions[0].store(3, Ordering::Release);
        assert!(!may_size_section(&owner, &revisions[0], 1));
        owner.deactivate();
        assert!(!may_size_section(&owner, &revisions[2], 2));
    }

    #[test]
    fn geometry_and_scroll_work_survive_an_unrelated_publication() {
        let owner = NativeChatOwner::new("host".into(), 1);
        let frame = owner.next_geometry();
        let scroll = owner.next_scroll();
        owner.set_generation(2);
        assert!(owner.may_set_geometry(frame));
        assert!(owner.may_scroll(scroll));
        let newer_frame = owner.next_geometry();
        assert!(!owner.may_set_geometry(frame));
        assert!(owner.may_set_geometry(newer_frame));
        owner.deactivate();
        assert!(!owner.may_set_geometry(newer_frame));
        assert!(!owner.may_scroll(scroll));
    }

    #[test]
    fn delayed_current_turn_callbacks_cross_the_child_bridge_but_obsolete_text_does_not() {
        let live = serde_json::json!({
            "key": "reply", "streaming": true, "generation": "g1",
            "contentEpoch": 7, "contentRevision": 9
        });
        let reasoning = serde_json::json!({
            "rowKey": "reply", "generation": "g1", "contentEpoch": 7
        });
        let drain = serde_json::json!({
            "rowKey": "reply", "generation": "g1", "contentEpoch": 7,
            "contentRevision": 9
        });
        let generation = serde_json::json!("g1");
        assert!(current_action_version(2, 3, 2, true));
        assert!(current_action_version(2, 3, 2, false));
        assert!(!current_action_version(3, 3, 2, false));
        assert!(current_turn_callback(
            "reasoning-open",
            &reasoning,
            &live,
            Some(&generation)
        ));
        assert!(current_turn_callback(
            "reveal-complete",
            &drain,
            &live,
            Some(&generation)
        ));

        let mut extended = live.clone();
        extended["contentRevision"] = serde_json::json!(10);
        assert!(current_turn_callback(
            "reasoning-open",
            &reasoning,
            &extended,
            Some(&generation)
        ));
        assert!(!current_turn_callback(
            "reveal-complete",
            &drain,
            &extended,
            Some(&generation)
        ));
        let mut replaced = live.clone();
        replaced["contentEpoch"] = serde_json::json!(11);
        replaced["contentRevision"] = serde_json::json!(11);
        assert!(!current_turn_callback(
            "reasoning-open",
            &reasoning,
            &replaced,
            Some(&generation)
        ));
        assert!(!current_turn_callback(
            "reveal-complete",
            &drain,
            &replaced,
            Some(&generation)
        ));
        assert!(!current_turn_callback(
            "reasoning-open",
            &reasoning,
            &live,
            Some(&serde_json::json!("g2"))
        ));
        assert!(!current_turn_callback(
            "reveal-complete",
            &drain,
            &serde_json::json!({ "key": "other", "streaming": true, "generation": "g2", "contentEpoch": 7, "contentRevision": 9 }),
            Some(&generation)
        ));
        assert!(!current_action_version(3, 3, 4, true));
    }
}
