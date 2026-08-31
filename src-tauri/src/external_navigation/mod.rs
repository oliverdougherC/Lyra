mod opener;
mod policy;

use tauri::{webview::WebviewWindowBuilder, App, Manager, Runtime};

pub(crate) use opener::{open_external_url, ExternalNavigationError};

const MAIN_WINDOW_LABEL: &str = "main";

pub(crate) fn create_main_window<R: Runtime>(app: &mut App<R>) -> tauri::Result<()> {
    if app.get_webview_window(MAIN_WINDOW_LABEL).is_some() {
        return Ok(());
    }

    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|window| window.label == MAIN_WINDOW_LABEL)
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                "main window config is missing from tauri.conf.json",
            )
        })?
        .clone();

    WebviewWindowBuilder::from_config(app.handle(), &config)?
        .on_navigation(policy::is_app_navigation_allowed)
        .on_new_window(|_url, _features| tauri::webview::NewWindowResponse::Deny)
        .build()?;

    Ok(())
}
