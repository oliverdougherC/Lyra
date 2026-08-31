use tauri::Url;

const APP_HTTPS_HOST: &str = "tauri.localhost";
const APP_TAURI_HOST: &str = "localhost";

pub(crate) fn is_app_navigation_allowed(url: &Url) -> bool {
    is_app_navigation_allowed_for_mode(url, cfg!(debug_assertions))
}

fn is_app_navigation_allowed_for_mode(url: &Url, allow_dev_loopback: bool) -> bool {
    match (url.scheme(), url.host_str()) {
        ("tauri", Some(APP_TAURI_HOST)) => true,
        ("https", Some(APP_HTTPS_HOST)) => true,
        ("http", Some(host)) | ("https", Some(host)) if allow_dev_loopback => {
            matches!(host, "localhost" | "127.0.0.1" | "::1")
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_packaged_app_origins() {
        assert!(is_app_navigation_allowed_for_mode(
            &Url::parse("https://tauri.localhost/classes/7").unwrap(),
            false,
        ));
        assert!(is_app_navigation_allowed_for_mode(
            &Url::parse("tauri://localhost/settings").unwrap(),
            false,
        ));
    }

    #[test]
    fn rejects_external_navigation() {
        assert!(!is_app_navigation_allowed_for_mode(
            &Url::parse("https://example.com/").unwrap(),
            false,
        ));
        assert!(!is_app_navigation_allowed_for_mode(
            &Url::parse("file:///tmp/lyra.html").unwrap(),
            false,
        ));
    }

    #[test]
    fn limits_dev_navigation_to_loopback() {
        assert!(is_app_navigation_allowed_for_mode(
            &Url::parse("http://127.0.0.1:1420/").unwrap(),
            true,
        ));
        assert!(is_app_navigation_allowed_for_mode(
            &Url::parse("http://localhost:3000/").unwrap(),
            true,
        ));
        assert!(!is_app_navigation_allowed_for_mode(
            &Url::parse("https://example.com/").unwrap(),
            true,
        ));
    }
}
