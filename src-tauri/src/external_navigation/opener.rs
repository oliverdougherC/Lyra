use std::fmt;
use std::net::ToSocketAddrs;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use tauri::{AppHandle, Runtime, Url};
use tauri_plugin_opener::OpenerExt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ExternalNavigationError {
    MalformedUrl,
    UnsupportedScheme,
    CredentialsNotAllowed,
    MissingHost,
    UnsafeHost,
    DnsVerificationFailed,
    BrowserOpenFailed,
}

impl fmt::Display for ExternalNavigationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MalformedUrl
            | Self::UnsupportedScheme
            | Self::UnsafeHost
            | Self::DnsVerificationFailed => {
                write!(f, "Lyra can only open public http or https links.")
            }
            Self::CredentialsNotAllowed => {
                write!(f, "Lyra can only open public http or https links.")
            }
            Self::MissingHost => write!(f, "Lyra can only open public http or https links."),
            Self::BrowserOpenFailed => {
                write!(f, "Lyra could not open that link in the system browser.")
            }
        }
    }
}

impl std::error::Error for ExternalNavigationError {}

pub(crate) fn open_external_url<R: Runtime>(
    app: &AppHandle<R>,
    candidate: &str,
) -> Result<(), ExternalNavigationError> {
    let normalized = normalize_public_http_url(candidate, resolve_host_ips)?;
    app.opener()
        .open_url(&normalized, None::<&str>)
        .map_err(|_| ExternalNavigationError::BrowserOpenFailed)
}

fn normalize_public_http_url<F>(
    candidate: &str,
    resolve_host: F,
) -> Result<String, ExternalNavigationError>
where
    F: Fn(&str, u16) -> Result<Vec<IpAddr>, ExternalNavigationError>,
{
    let trimmed = candidate.trim();
    if trimmed.is_empty() || trimmed != candidate {
        return Err(ExternalNavigationError::MalformedUrl);
    }

    let url = Url::parse(candidate).map_err(|_| ExternalNavigationError::MalformedUrl)?;

    match url.scheme() {
        "http" | "https" => {}
        _ => return Err(ExternalNavigationError::UnsupportedScheme),
    }

    if !url.username().is_empty() || url.password().is_some() {
        return Err(ExternalNavigationError::CredentialsNotAllowed);
    }

    let host = url.host_str().ok_or(ExternalNavigationError::MissingHost)?;
    let host = host.trim_start_matches('[').trim_end_matches(']');
    if let Ok(ip) = host.parse::<IpAddr>() {
        if !is_safe_public_ip(ip) {
            return Err(ExternalNavigationError::UnsafeHost);
        }
    } else if !is_safe_public_domain(host) {
        return Err(ExternalNavigationError::UnsafeHost);
    } else {
        let port = url
            .port_or_known_default()
            .ok_or(ExternalNavigationError::MalformedUrl)?;
        let resolved = resolve_host(host, port)?;
        if resolved.is_empty() || resolved.iter().any(|ip| !is_safe_public_ip(*ip)) {
            return Err(ExternalNavigationError::DnsVerificationFailed);
        }
    }

    Ok(url.to_string())
}

fn resolve_host_ips(host: &str, port: u16) -> Result<Vec<IpAddr>, ExternalNavigationError> {
    (host, port)
        .to_socket_addrs()
        .map_err(|_| ExternalNavigationError::DnsVerificationFailed)
        .map(|addresses| addresses.map(|address| address.ip()).collect())
}

fn is_safe_public_domain(domain: &str) -> bool {
    let lower = domain.to_ascii_lowercase();
    if lower.is_empty()
        || lower == "localhost"
        || lower.ends_with(".localhost")
        || lower.ends_with(".local")
        || lower.ends_with(".internal")
    {
        return false;
    }

    lower.contains('.')
}

fn is_safe_public_ip(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_safe_public_ipv4(address),
        IpAddr::V6(address) => {
            if let Some(mapped) = address.to_ipv4_mapped() {
                return is_safe_public_ipv4(mapped);
            }
            is_safe_public_ipv6(address)
        }
    }
}

fn is_safe_public_ipv4(address: Ipv4Addr) -> bool {
    let [first, second, ..] = address.octets();

    !(address.is_private()
        || address.is_loopback()
        || address.is_link_local()
        || address.is_broadcast()
        || address.is_documentation()
        || address.is_unspecified()
        || address.is_multicast()
        || first == 0
        || first >= 240
        || (first == 100 && (64..=127).contains(&second))
        || (first == 198 && matches!(second, 18 | 19)))
}

fn is_safe_public_ipv6(address: Ipv6Addr) -> bool {
    let segments = address.segments();

    !(address.is_loopback()
        || address.is_unspecified()
        || address.is_multicast()
        || (segments[0] == 0x2001 && segments[1] == 0x0db8)
        || (segments[0] == 0x2001 && segments[1] <= 0x01ff)
        || ((segments[0] & 0xfe00) == 0xfc00)
        || ((segments[0] & 0xffc0) == 0xfe80)
        || ((segments[0] & 0xffc0) == 0xfec0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_public_urls() {
        assert_eq!(
            normalize_public_http_url("https://example.com/docs", |_host, _port| {
                Ok(vec![IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))])
            })
            .unwrap(),
            "https://example.com/docs"
        );
        assert_eq!(
            normalize_public_http_url("http://8.8.8.8", |_host, _port| Ok(Vec::new())).unwrap(),
            "http://8.8.8.8/"
        );
        assert_eq!(
            normalize_public_http_url("https://[2606:4700:4700::1111]/", |_host, _port| {
                Ok(Vec::new())
            })
            .unwrap(),
            "https://[2606:4700:4700::1111]/"
        );
    }

    #[test]
    fn rejects_bad_schemes_and_credentials() {
        assert_eq!(
            normalize_public_http_url("javascript:alert(1)", |_host, _port| Ok(Vec::new()))
                .unwrap_err(),
            ExternalNavigationError::UnsupportedScheme,
        );
        assert_eq!(
            normalize_public_http_url("https://user:secret@example.com/", |_host, _port| {
                Ok(vec![IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34))])
            })
            .unwrap_err(),
            ExternalNavigationError::CredentialsNotAllowed,
        );
        assert_eq!(
            normalize_public_http_url(" https://example.com", |_host, _port| Ok(Vec::new()))
                .unwrap_err(),
            ExternalNavigationError::MalformedUrl,
        );
    }

    #[test]
    fn rejects_local_and_reserved_hosts() {
        for candidate in [
            "http://127.0.0.1:8000/",
            "https://localhost/",
            "https://printer/",
            "https://10.1.2.3/",
            "https://172.16.4.1/",
            "https://192.168.0.5/",
            "https://169.254.10.2/",
            "https://100.64.1.2/",
            "https://198.18.0.1/",
            "https://240.0.0.1/",
            "https://[::1]/",
            "https://[fe80::1]/",
            "https://[fc00::1]/",
            "https://[2001:db8::1]/",
        ] {
            assert_eq!(
                normalize_public_http_url(candidate, |_host, _port| Ok(Vec::new())).unwrap_err(),
                ExternalNavigationError::UnsafeHost,
                "candidate {candidate} should be rejected as unsafe",
            );
        }
    }

    #[test]
    fn rejects_non_http_schemes() {
        for candidate in [
            "file:///tmp/test.txt",
            "data:text/plain,hello",
            "blob:https://example.com/id",
            "mailto:test@example.com",
        ] {
            assert_eq!(
                normalize_public_http_url(candidate, |_host, _port| Ok(Vec::new())).unwrap_err(),
                ExternalNavigationError::UnsupportedScheme,
                "candidate {candidate} should be rejected for scheme",
            );
        }
    }

    #[test]
    fn rejects_unresolvable_or_mixed_dns_answers() {
        assert_eq!(
            normalize_public_http_url("https://example.com", |_host, _port| Ok(Vec::new()))
                .unwrap_err(),
            ExternalNavigationError::DnsVerificationFailed,
        );
        assert_eq!(
            normalize_public_http_url("https://example.com", |_host, _port| {
                Ok(vec![
                    IpAddr::V4(Ipv4Addr::new(93, 184, 216, 34)),
                    IpAddr::V4(Ipv4Addr::LOCALHOST),
                ])
            })
            .unwrap_err(),
            ExternalNavigationError::DnsVerificationFailed,
        );
    }
}
