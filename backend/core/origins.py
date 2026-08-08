"""Loopback browser origins trusted by Lyra's local HTTP boundary."""

ALLOWED_BROWSER_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

ALLOWED_BROWSER_ORIGIN_SET = frozenset(ALLOWED_BROWSER_ORIGINS)
