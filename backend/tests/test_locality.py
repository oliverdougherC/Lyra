"""Endpoint locality. Resolution is faked so the suite is deterministic and offline."""

import socket

import pytest

from backend.llm.locality import is_local_endpoint

RESOLUTIONS = {
    "127.0.0.1": ["127.0.0.1"],
    "::1": ["::1"],
    "api.example.com": ["93.184.216.34"],
    # A host that resolves to both loopback and a routable address. Sending document
    # text there could leave the machine, so it is not local.
    "split.example.com": ["127.0.0.1", "10.0.0.5"],
}


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer only from the table above; anything else fails to resolve."""

    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list[tuple]:
        try:
            addresses = RESOLUTIONS[host]
        except KeyError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known") from None
        return [
            (socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", (address, 0)) for address in addresses
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8080/v1", True),
        # `localhost` is decided without resolution: it is absent from the table, so a
        # False here would mean the short circuit was lost.
        ("http://localhost:8080/v1", True),
        ("http://[::1]:8080/v1", True),
        ("https://api.example.com/v1", False),
        ("http://split.example.com:8080/v1", False),
        # Unresolvable is treated as remote, which is the safe direction.
        ("http://nowhere.invalid/v1", False),
        ("", False),
    ],
)
def test_endpoint_locality(url: str, expected: bool) -> None:
    assert is_local_endpoint(url) is expected
