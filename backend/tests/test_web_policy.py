"""URL policy tests stay offline by faking DNS answers."""

from __future__ import annotations

import socket

import pytest

from backend.core import web_policy

RESOLUTIONS = {
    "loopback.example.test": ["127.0.0.1"],
    "split-loopback.example.test": ["127.0.0.1", "93.184.216.34"],
    "public.example.test": ["93.184.216.34"],
    "private.example.test": ["10.0.0.12"],
}


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        try:
            addresses = RESOLUTIONS[host]
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (address, int(port or 0)))
            for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_firecrawl_base_requires_loopback_and_a_clean_base_url() -> None:
    details = web_policy.validate_firecrawl_base_url("http://loopback.example.test:3002")
    assert details.normalized_url == "http://loopback.example.test:3002"
    assert details.port == 3002

    with pytest.raises(web_policy.LoopbackPolicyError):
        web_policy.validate_firecrawl_base_url("http://public.example.test:3002")

    with pytest.raises(web_policy.LoopbackPolicyError):
        web_policy.validate_firecrawl_base_url("http://split-loopback.example.test:3002")

    with pytest.raises(web_policy.LoopbackPolicyError):
        web_policy.validate_firecrawl_base_url("http://loopback.example.test:3002/v2")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        "http://localhost/admin",
        "http://private.example.test/admin",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/admin",
        "http://course.local/resource",
        "https://example.com:99999/",
    ],
)
def test_public_targets_reject_non_public_or_malformed_urls(url: str) -> None:
    with pytest.raises(web_policy.URLPolicyError):
        web_policy.validate_firecrawl_target_url(url)


def test_target_and_final_url_validators_accept_public_http_urls() -> None:
    target = web_policy.validate_firecrawl_target_url("https://public.example.test/readme")
    final = web_policy.validate_firecrawl_final_url("https://public.example.test/final")
    assert target.normalized_url == "https://public.example.test/readme"
    assert final.normalized_url == "https://public.example.test/final"
