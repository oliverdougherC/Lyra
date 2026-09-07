mod opener;
mod policy;

use tauri::utils::config::WindowConfig;
use tauri::{webview::WebviewWindowBuilder, App, Manager, Runtime};

pub(crate) use opener::{open_external_url, ExternalNavigationError};

const MAIN_WINDOW_LABEL: &str = "main";

pub(crate) fn create_main_window<R: Runtime>(app: &mut App<R>) -> tauri::Result<()> {
    if app.get_webview_window(MAIN_WINDOW_LABEL).is_some() {
        return Ok(());
    }

    let mut config = app
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

    if let Some(monitor) = app.primary_monitor()? {
        let area = monitor.work_area();
        let scale = monitor.scale_factor();
        fit_main_window(
            &mut config,
            f64::from(area.size.width) / scale,
            f64::from(area.size.height) / scale,
            f64::from(area.position.x) / scale,
            f64::from(area.position.y) / scale,
        );
    }

    WebviewWindowBuilder::from_config(app.handle(), &config)?
        .on_navigation(policy::is_app_navigation_allowed)
        .on_new_window(|_url, _features| tauri::webview::NewWindowResponse::Deny)
        .build()?;

    Ok(())
}

fn fit_main_window(config: &mut WindowConfig, width: f64, height: f64, x: f64, y: f64) {
    // Config dimensions describe content. Leave room for the native title bar and
    // a small inset inside the work area (which already excludes Dock/menu bar).
    config.width = config.width.min((width - 32.0).max(1.0));
    config.height = config.height.min((height - 72.0).max(1.0));
    config.min_width = config.min_width.map(|minimum| minimum.min(config.width));
    config.min_height = config.min_height.map(|minimum| minimum.min(config.height));
    config.x = Some(x + (width - config.width) / 2.0);
    config.y = Some(y + 16.0);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn laptop_work_area_keeps_title_bar_and_controls_visible() {
        let mut config = WindowConfig {
            width: 1400.0,
            height: 960.0,
            min_width: Some(540.0),
            min_height: Some(600.0),
            ..Default::default()
        };
        fit_main_window(&mut config, 1280.0, 720.0, -1280.0, 25.0);
        assert_eq!((config.width, config.height), (1248.0, 648.0));
        assert_eq!((config.x, config.y), (Some(-1264.0), Some(41.0)));
        assert_eq!(
            (config.min_width, config.min_height),
            (Some(540.0), Some(600.0))
        );
    }

    #[test]
    fn unusually_small_work_area_does_not_force_window_offscreen() {
        let mut config = WindowConfig {
            min_width: Some(540.0),
            min_height: Some(600.0),
            ..Default::default()
        };
        fit_main_window(&mut config, 500.0, 600.0, 0.0, 0.0);
        assert!(config.width <= 468.0 && config.height <= 528.0);
        assert!(config.min_width.unwrap() <= config.width);
        assert!(config.min_height.unwrap() <= config.height);
    }
}
