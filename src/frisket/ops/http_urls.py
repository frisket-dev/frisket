"""Pure HTTP(S) input syntax shared by URL routing and explicit downloads.

This is not an egress/security policy: transports still enforce their own policy.
"""

from urllib.parse import ParseResult, urlparse


def parse_http_url(url: str) -> tuple[ParseResult, str] | None:
    """Return parsed parts and the routing host, or None for unsupported syntax."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
    return (parsed, host) if host else None


def is_http_url(url: str) -> bool:
    return parse_http_url(url) is not None
