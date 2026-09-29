//! macOS chat transcript displayed by a WKWebView inside an AppKit scroll view.
//! The main webview remains the owner of conversations, streaming, and navigation.

use serde::Deserialize;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, Webview};

#[derive(Default)]
pub struct NativeChatState {
    inner: Arc<Mutex<Option<NativeChatHandle>>>,
}

#[cfg(target_os = "macos")]
static CHAT_APP: std::sync::OnceLock<AppHandle> = std::sync::OnceLock::new();

#[cfg(target_os = "macos")]
#[no_mangle]
pub extern "C" fn lyra_native_chat_scroll_changed(at_bottom: bool) {
    if let Some(app) = CHAT_APP.get() {
        if let Some(main) = app.get_webview("main") {
            let _ = main.eval(format!(
                "window.dispatchEvent(new CustomEvent('lyra:native-scroll-state', {{ detail: {{ atBottom: {at_bottom} }} }}))"
            ));
        }
    }
}

struct NativeChatHandle {
    webviews: Vec<Webview>,
    loaded: Vec<bool>,
    ready: Vec<bool>,
    last_sections: Vec<serde_json::Value>,
    scope: String,
    version: u64,
    #[cfg(target_os = "macos")]
    scroll: usize,
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
        fn lyra_native_chat_set_visible(scroll: *mut c_void, visible: bool);
        fn lyra_native_chat_detach(scroll: *mut c_void);
    }

    fn handle(app: &AppHandle) -> Result<usize, String> {
        let state = app.state::<NativeChatState>();
        let _ = CHAT_APP.set(app.clone());
        let guard = state
            .inner
            .lock()
            .map_err(|_| "Chat scroll state is unavailable")?;
        let current = guard.as_ref().ok_or("The native chat view is not open")?;
        Ok(current.scroll)
    }

    #[tauri::command]
    pub async fn native_chat_mount(
        app: AppHandle,
        webview: Webview,
        rect: ChatRect,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        if !rect.valid() {
            return Err("The chat viewport has invalid dimensions.".into());
        }
        let state = app.state::<NativeChatState>();
        let inner = Arc::clone(&state.inner);
        tauri::async_runtime::spawn_blocking(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            if let Some(existing) = guard.as_ref() {
                let scroll = existing.scroll;
                app.run_on_main_thread(move || unsafe {
                    lyra_native_chat_set_frame(
                        scroll as *mut c_void,
                        rect.x,
                        rect.top,
                        rect.width,
                        rect.height,
                    );
                })
                .map_err(|_| "The chat viewport could not be positioned")?;
                return Ok(());
            }

            let window = app
                .get_window("main")
                .ok_or("The desktop window is unavailable")?;
            let builder = WebviewBuilder::new(
                "native-chat-0",
                WebviewUrl::App("native-transcript.html".into()),
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
                        )
                    };
                    let _ = tx.send(scroll as usize);
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
                webviews: vec![child],
                loaded: vec![false],
                ready: vec![false],
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
        rect: ChatRect,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        if !rect.valid() {
            return Err("The chat viewport has invalid dimensions.".into());
        }
        let scroll = handle(&app)?;
        app.run_on_main_thread(move || unsafe {
            lyra_native_chat_set_frame(
                scroll as *mut c_void,
                rect.x,
                rect.top,
                rect.width,
                rect.height,
            );
        })
        .map_err(|_| "The chat viewport could not be positioned".into())
    }

    #[tauri::command]
    pub async fn native_chat_set_content_height(
        app: AppHandle,
        webview: Webview,
        height: f64,
    ) -> Result<(), String> {
        let index = section_index(&webview)?;
        if !height.is_finite() || !(1.0..=18_000.0).contains(&height) {
            return Err("The chat content has invalid dimensions.".into());
        }
        let scroll = {
            let state = app.state::<NativeChatState>();
            let guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_ref().ok_or("The native chat view is not open")?;
            if current.webviews.get(index).map(Webview::label) != Some(webview.label()) {
                return Err("The chat section is no longer open.".into());
            }
            current.scroll
        };
        app.run_on_main_thread(move || unsafe {
            lyra_native_chat_set_section_height(scroll as *mut c_void, index, height);
        })
        .map_err(|_| "The chat content could not be sized".into())
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
            if version <= current.version {
                return Ok(());
            }
            current.version = version;
            if scope != current.scope {
                current.scope = scope;
                current.ready.fill(false);
                current.last_sections.fill(serde_json::Value::Null);
                let scroll = current.scroll;
                app.run_on_main_thread(move || unsafe {
                    lyra_native_chat_set_visible(scroll as *mut c_void, false);
                })
                .map_err(|_| "The previous chat could not be hidden")?;
            }
            let window = app
                .get_window("main")
                .ok_or("The desktop window is unavailable")?;
            while current.webviews.len() < count {
                let index = current.webviews.len();
                let label = format!("native-chat-{index}");
                let builder =
                    WebviewBuilder::new(label, WebviewUrl::App("native-transcript.html".into()))
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
                let (tx, rx) = mpsc::channel();
                child
                    .with_webview(move |platform| {
                        let result = unsafe {
                            lyra_native_chat_add_section(scroll as *mut c_void, platform.inner())
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
                current.last_sections.push(serde_json::Value::Null);
            }
            while current.webviews.len() > count {
                let child = current.webviews.pop().expect("section exists");
                current.loaded.pop();
                current.ready.pop();
                current.last_sections.pop();
                let scroll = current.scroll;
                let (tx, rx) = mpsc::channel();
                app.run_on_main_thread(move || {
                    unsafe { lyra_native_chat_remove_last_section(scroll as *mut c_void) };
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
        if matches!(kind, "ready") {
            let state = app.state::<NativeChatState>();
            let mut guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_mut().ok_or("The native chat view is not open")?;
            if current.webviews.get(index).map(Webview::label) != Some(webview.label()) {
                return Err("The chat section is no longer open.".into());
            }
            current.loaded[index] = true;
            current.last_sections[index] = serde_json::Value::Null;
        }
        if matches!(kind, "content-ready") {
            let state = app.state::<NativeChatState>();
            let mut guard = state
                .inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let current = guard.as_mut().ok_or("The native chat view is not open")?;
            if action.get("scope").and_then(serde_json::Value::as_str)
                != Some(current.scope.as_str())
            {
                return Ok(());
            }
            if current.webviews.get(index).map(Webview::label) != Some(webview.label()) {
                return Err("The chat section is no longer open.".into());
            }
            current.ready[index] = true;
            if !current.ready.iter().all(|ready| *ready) {
                return Ok(());
            }
        }
        let payload = serde_json::to_string(&action).map_err(|_| "The chat action is invalid")?;
        if payload.len() > 4096 {
            return Err("The chat action is too large.".into());
        }
        let main = app
            .get_webview("main")
            .ok_or("The desktop view is unavailable")?;
        main.eval(format!(
            "window.dispatchEvent(new CustomEvent('lyra:native-chat-action', {{ detail: {payload} }}))"
        ))
        .map_err(|_| "The chat action could not be delivered".into())
    }

    #[tauri::command]
    pub async fn native_chat_scroll_to_bottom(
        app: AppHandle,
        webview: Webview,
    ) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let scroll = handle(&app)?;
        app.run_on_main_thread(move || unsafe {
            lyra_native_chat_scroll_to_bottom(scroll as *mut c_void);
        })
        .map_err(|_| "The chat could not be scrolled".into())
    }

    #[tauri::command]
    pub async fn native_chat_show(app: AppHandle, webview: Webview) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let scroll = handle(&app)?;
        app.run_on_main_thread(move || unsafe {
            lyra_native_chat_set_visible(scroll as *mut c_void, true);
        })
        .map_err(|_| "The chat view could not be shown".into())
    }

    #[tauri::command]
    pub async fn native_chat_unmount(app: AppHandle, webview: Webview) -> Result<(), String> {
        caller_is(&webview, "main")?;
        let state = app.state::<NativeChatState>();
        let inner = Arc::clone(&state.inner);
        tauri::async_runtime::spawn_blocking(move || {
            let mut guard = inner
                .lock()
                .map_err(|_| "Chat scroll state is unavailable")?;
            let Some(current) = guard.take() else {
                return Ok(());
            };
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
    pub async fn native_chat_mount(_: AppHandle, _: Webview, _: ChatRect) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_set_frame(
        _: AppHandle,
        _: Webview,
        _: ChatRect,
    ) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_set_content_height(
        _: AppHandle,
        _: Webview,
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
    pub async fn native_chat_scroll_to_bottom(_: AppHandle, _: Webview) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_show(_: AppHandle, _: Webview) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
    #[tauri::command]
    pub async fn native_chat_unmount(_: AppHandle, _: Webview) -> Result<(), String> {
        Err("Native chat scrolling requires macOS.".into())
    }
}

#[cfg(not(target_os = "macos"))]
pub use unsupported::*;
