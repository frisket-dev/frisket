"""Static URL capture for row-local web evidence."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Mapping
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import trafilatura

from frisket.ops.netguard import url_is_safe


DEFAULT_STATIC_CAPTURE_MAX_BYTES = 5_000_000
DEFAULT_STATIC_CAPTURE_TIMEOUT_MS = 30_000
MAX_STATIC_CAPTURE_REDIRECTS = 10
STATIC_CAPTURE_USER_AGENT = "frisket/url-capture-static"
DEFAULT_BROWSER_CAPTURE_MAX_BYTES = 25_000_000
DEFAULT_BROWSER_CAPTURE_TIMEOUT_MS = 30_000
MAX_BROWSER_CAPTURE_REQUESTS = 100
BROWSER_CAPTURE_USER_AGENT = "frisket/url-capture-browser"
WARC_MIME = "application/warc"


@dataclass
class StaticUrlFetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int | None = None
    redirects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StaticUrlCaptureResult:
    status: str
    input_url: str
    final_url: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    captured_at: str | None = None
    html_bytes: bytes | None = None
    markdown: str | None = None
    links: list[dict[str, str]] = field(default_factory=list)
    content_type: str | None = None
    status_code: int | None = None
    byte_count: int = 0
    network_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    reason: str | None = None
    extractor_name: str = "trafilatura"
    extractor_version: str | None = None
    render_mode: str = "static"
    screenshot_bytes: bytes | None = None
    screenshot_mime: str | None = None
    warc_bytes: bytes | None = None
    warc_mime: str | None = None


@dataclass
class BrowserUrlResource:
    url: str
    method: str = "GET"
    status_code: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    elapsed_ms: int | None = None


@dataclass
class BrowserUrlRenderResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    html: str = ""
    screenshot: bytes = b""
    screenshot_mime: str = "image/png"
    elapsed_ms: int | None = None
    resources: list[BrowserUrlResource | Mapping[str, Any]] = field(
        default_factory=list
    )


StaticUrlFetchFn = Callable[..., StaticUrlFetchResult]
BrowserUrlRenderFn = Callable[..., BrowserUrlRenderResult]


def capture_static_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_STATIC_CAPTURE_MAX_BYTES,
    timeout_ms: int = DEFAULT_STATIC_CAPTURE_TIMEOUT_MS,
    fetch: StaticUrlFetchFn | None = None,
) -> StaticUrlCaptureResult:
    input_url = url.strip()
    extractor_version = _trafilatura_version()
    parsed = urlparse(input_url)
    if parsed.scheme not in {"http", "https"}:
        return _error_result(
            input_url,
            "URL must start with http:// or https://",
            reason="unsupported_url_scheme",
            extractor_version=extractor_version,
        )
    if not url_is_safe(input_url):
        return _error_result(
            input_url,
            f"blocked URL (private/loopback/metadata): {input_url}",
            reason="unsafe_url",
            extractor_version=extractor_version,
        )

    try:
        fetched = (fetch or fetch_static_url)(
            input_url,
            max_bytes=max_bytes,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001 - row-local failure is intentional
        return _error_result(
            input_url,
            f"{type(exc).__name__}: {exc}",
            reason="fetch_failed",
            extractor_version=extractor_version,
        )

    final_url = fetched.final_url or input_url
    content_type = _content_type(fetched.headers)
    byte_count = len(fetched.body)
    network_metadata = _network_metadata(
        input_url=input_url,
        final_url=final_url,
        status_code=fetched.status_code,
        content_type=content_type,
        byte_count=byte_count,
        elapsed_ms=fetched.elapsed_ms,
        redirects=fetched.redirects,
    )
    if fetched.status_code >= 400:
        return _error_result(
            input_url,
            f"HTTP {fetched.status_code}",
            reason="http_error",
            final_url=final_url,
            status_code=fetched.status_code,
            byte_count=byte_count,
            network_metadata=network_metadata,
            extractor_version=extractor_version,
        )
    if content_type and content_type not in {"text/html", "application/xhtml+xml"}:
        return _error_result(
            input_url,
            f"unsupported content type for static page capture: {content_type}",
            reason="unsupported_content_type",
            final_url=final_url,
            status_code=fetched.status_code,
            byte_count=byte_count,
            network_metadata=network_metadata,
            extractor_version=extractor_version,
        )
    if not fetched.body:
        return _error_result(
            input_url,
            "HTTP response body is empty",
            reason="empty_body",
            final_url=final_url,
            status_code=fetched.status_code,
            byte_count=0,
            network_metadata=network_metadata,
            extractor_version=extractor_version,
        )

    html_text = _decode_html(fetched.body, fetched.headers)
    metadata = extract_html_metadata(html_text, base_url=final_url)
    links = extract_html_links(html_text, base_url=final_url)
    markdown, warnings = extract_markdown(html_text)
    return StaticUrlCaptureResult(
        status="captured",
        input_url=input_url,
        final_url=final_url,
        canonical_url=metadata.get("canonical_url") or final_url,
        title=metadata.get("title"),
        author=metadata.get("author"),
        published_at=metadata.get("published_at"),
        captured_at=_utc_now(),
        html_bytes=fetched.body,
        markdown=markdown,
        links=links,
        content_type=content_type or "text/html",
        status_code=fetched.status_code,
        byte_count=byte_count,
        network_metadata=network_metadata,
        warnings=warnings,
        extractor_version=extractor_version,
        render_mode="static",
    )


def capture_playwright_url(
    url: str,
    *,
    capture_screenshot: bool,
    include_warc: bool = False,
    full_page: bool = True,
    viewport_width: int | None = None,
    viewport_height: int | None = None,
    max_bytes: int = DEFAULT_BROWSER_CAPTURE_MAX_BYTES,
    timeout_ms: int = DEFAULT_BROWSER_CAPTURE_TIMEOUT_MS,
    render: BrowserUrlRenderFn | None = None,
) -> StaticUrlCaptureResult:
    input_url = url.strip()
    extractor_version = _trafilatura_version()
    parsed = urlparse(input_url)
    if parsed.scheme not in {"http", "https"}:
        return _error_result(
            input_url,
            "URL must start with http:// or https://",
            reason="unsupported_url_scheme",
            extractor_version=extractor_version,
            render_mode="playwright",
        )
    if not url_is_safe(input_url):
        return _error_result(
            input_url,
            f"blocked URL (private/loopback/metadata): {input_url}",
            reason="unsafe_url",
            extractor_version=extractor_version,
            render_mode="playwright",
        )

    try:
        render_kwargs: dict[str, Any] = {
            "max_bytes": max_bytes,
            "timeout_ms": timeout_ms,
            "full_page": full_page,
            "capture_screenshot": capture_screenshot,
        }
        if viewport_width is not None:
            render_kwargs["viewport_width"] = viewport_width
        if viewport_height is not None:
            render_kwargs["viewport_height"] = viewport_height
        rendered = (render or render_playwright_url)(input_url, **render_kwargs)
    except Exception as exc:  # noqa: BLE001 - row-local failure is intentional
        return _error_result(
            input_url,
            f"{type(exc).__name__}: {exc}",
            reason="browser_render_failed",
            extractor_version=extractor_version,
            render_mode="playwright",
        )

    final_url = rendered.final_url or input_url
    if not url_is_safe(final_url):
        return _error_result(
            input_url,
            f"blocked browser final URL (private/loopback/metadata): {final_url}",
            reason="unsafe_final_url",
            final_url=final_url,
            status_code=rendered.status_code,
            extractor_version=extractor_version,
            render_mode="playwright",
        )
    if not rendered.html.strip():
        return _error_result(
            input_url,
            "Browser rendered HTML is empty",
            reason="empty_body",
            final_url=final_url,
            status_code=rendered.status_code,
            extractor_version=extractor_version,
            render_mode="playwright",
        )
    if capture_screenshot and not rendered.screenshot:
        return _error_result(
            input_url,
            "Browser capture did not produce a screenshot",
            reason="missing_screenshot",
            final_url=final_url,
            status_code=rendered.status_code,
            extractor_version=extractor_version,
            render_mode="playwright",
        )

    html_bytes = rendered.html.encode("utf-8")
    resources = [_browser_resource(item) for item in rendered.resources]
    screenshot_bytes = rendered.screenshot if capture_screenshot else b""
    aggregate_bytes = len(html_bytes) + len(screenshot_bytes)
    aggregate_bytes += sum(len(resource.body or b"") for resource in resources)
    if aggregate_bytes > max_bytes:
        return _error_result(
            input_url,
            "browser capture exceeds max_bytes",
            reason="browser_capture_too_large",
            final_url=final_url,
            status_code=rendered.status_code,
            byte_count=aggregate_bytes,
            extractor_version=extractor_version,
            render_mode="playwright",
        )

    content_type = _content_type(rendered.headers) or "text/html"
    metadata = extract_html_metadata(rendered.html, base_url=final_url)
    links = extract_html_links(rendered.html, base_url=final_url)
    markdown, warnings = extract_markdown(rendered.html)
    network_metadata = _browser_network_metadata(
        input_url=input_url,
        final_url=final_url,
        status_code=rendered.status_code,
        content_type=content_type,
        html_byte_count=len(html_bytes),
        screenshot_byte_count=len(screenshot_bytes),
        aggregate_byte_count=aggregate_bytes,
        elapsed_ms=rendered.elapsed_ms,
        resources=resources,
        include_warc=include_warc,
    )
    warc_bytes = None
    if include_warc:
        warc_bytes = build_warc(
            final_url=final_url,
            status_code=rendered.status_code,
            headers=rendered.headers,
            html_bytes=html_bytes,
            resources=resources,
        )
        network_metadata["warc_record_count"] = _warc_record_count(warc_bytes)
    return StaticUrlCaptureResult(
        status="captured",
        input_url=input_url,
        final_url=final_url,
        canonical_url=metadata.get("canonical_url") or final_url,
        title=metadata.get("title"),
        author=metadata.get("author"),
        published_at=metadata.get("published_at"),
        captured_at=_utc_now(),
        html_bytes=html_bytes,
        markdown=markdown,
        links=links,
        content_type=content_type,
        status_code=rendered.status_code,
        byte_count=len(html_bytes),
        network_metadata=network_metadata,
        warnings=warnings,
        extractor_version=extractor_version,
        render_mode="playwright",
        screenshot_bytes=screenshot_bytes or None,
        screenshot_mime=(rendered.screenshot_mime or "image/png")
        if screenshot_bytes
        else None,
        warc_bytes=warc_bytes,
        warc_mime=WARC_MIME if warc_bytes is not None else None,
    )


def render_playwright_url(
    url: str,
    *,
    capture_screenshot: bool,
    max_bytes: int = DEFAULT_BROWSER_CAPTURE_MAX_BYTES,
    timeout_ms: int = DEFAULT_BROWSER_CAPTURE_TIMEOUT_MS,
    full_page: bool = True,
    viewport_width: int = 1280,
    viewport_height: int = 720,
) -> BrowserUrlRenderResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised only without fixture
        # Both steps, because having only the binaries is the common
        # half-installed state: the web e2e suite's `npx playwright install
        # chromium` fills ~/.cache/ms-playwright without the Python package.
        raise RuntimeError(
            "Dynamic capture needs Python Playwright, which is not installed:\n"
            "  pip install 'frisket[browser]'\n"
            "  playwright install chromium"
        ) from exc

    start = time.monotonic()
    resources: list[BrowserUrlResource] = []
    aggregate = 0
    request_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=BROWSER_CAPTURE_USER_AGENT,
                viewport={"width": viewport_width, "height": viewport_height},
            )
            page = context.new_page()

            def route_handler(route: Any) -> None:
                nonlocal request_count
                request_count += 1
                request_url = route.request.url
                if request_count > MAX_BROWSER_CAPTURE_REQUESTS:
                    route.abort()
                    return
                if not url_is_safe(request_url):
                    route.abort()
                    return
                route.continue_()

            def response_handler(response: Any) -> None:
                nonlocal aggregate
                body: bytes | None = None
                try:
                    candidate = response.body()
                except Exception:  # noqa: BLE001 - subresource body is optional
                    candidate = b""
                if candidate and aggregate + len(candidate) <= max_bytes:
                    body = candidate
                    aggregate += len(candidate)
                resources.append(
                    BrowserUrlResource(
                        url=response.url,
                        method=response.request.method,
                        status_code=response.status,
                        headers=dict(response.headers),
                        body=body,
                    )
                )

            page.route("**/*", route_handler)
            page.on("response", response_handler)
            response = page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            html = page.content()
            html_bytes = html.encode("utf-8")
            if aggregate + len(html_bytes) > max_bytes:
                raise ValueError("browser rendered HTML exceeds max_bytes")
            aggregate += len(html_bytes)
            screenshot = b""
            if capture_screenshot:
                screenshot = page.screenshot(full_page=full_page, type="png")
                if aggregate + len(screenshot) > max_bytes:
                    raise ValueError("browser screenshot exceeds max_bytes")
            final_url = page.url
            status_code = int(response.status) if response is not None else 0
            headers = dict(response.headers) if response is not None else {}
            return BrowserUrlRenderResult(
                requested_url=url,
                final_url=final_url,
                status_code=status_code,
                headers=headers,
                html=html,
                screenshot=screenshot,
                screenshot_mime="image/png",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                resources=resources[:MAX_BROWSER_CAPTURE_REQUESTS],
            )
        finally:
            browser.close()


def fetch_static_url(
    url: str,
    *,
    max_bytes: int = DEFAULT_STATIC_CAPTURE_MAX_BYTES,
    timeout_ms: int = DEFAULT_STATIC_CAPTURE_TIMEOUT_MS,
) -> StaticUrlFetchResult:
    start = time.monotonic()
    current = url
    redirects: list[dict[str, Any]] = []
    timeout = max(timeout_ms / 1000.0, 0.001)
    http_timeout = httpx.Timeout(
        timeout,
        connect=min(timeout, 10.0),
        read=timeout,
        write=min(timeout, 10.0),
        pool=min(timeout, 10.0),
    )
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=0)
    with httpx.Client(
        follow_redirects=False,
        timeout=http_timeout,
        limits=limits,
    ) as client:
        while True:
            if not url_is_safe(current):
                raise ValueError(f"blocked URL (private/loopback/metadata): {current}")
            with client.stream(
                "GET",
                current,
                headers={"User-Agent": STATIC_CAPTURE_USER_AGENT},
            ) as response:
                if response.is_redirect:
                    if len(redirects) >= MAX_STATIC_CAPTURE_REDIRECTS:
                        raise ValueError("too many redirects")
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("redirect missing Location header")
                    next_url = str(response.next_request.url)
                    if not url_is_safe(next_url):
                        raise ValueError(f"blocked redirect target: {next_url}")
                    redirects.append(
                        {
                            "status_code": response.status_code,
                            "url_hash": text_hash(current),
                            "location_hash": text_hash(next_url),
                        }
                    )
                    current = next_url
                    continue
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("response body exceeds max_bytes")
                    chunks.append(chunk)
                return StaticUrlFetchResult(
                    requested_url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=b"".join(chunks),
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    redirects=redirects,
                )


def extract_markdown(html: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    markdown = trafilatura.extract(
        html,
        output_format="markdown",
        with_metadata=True,
        include_tables=True,
        include_links=True,
        deduplicate=True,
    )
    if markdown and markdown.strip():
        return markdown.strip() + "\n", warnings
    warnings.append("trafilatura_empty_output")
    fallback = _VisibleTextParser()
    fallback.feed(html)
    text = fallback.text()
    return (text + "\n") if text else "", warnings


def extract_html_metadata(html: str, *, base_url: str) -> dict[str, str]:
    parser = _MetadataParser(base_url=base_url)
    parser.feed(html)
    return parser.metadata()


def extract_html_links(html: str, *, base_url: str) -> list[dict[str, str]]:
    """Return unique navigable anchors in document order.

    A harvested link is an absolute HTTP(S) URL. Fragments do not identify a
    distinct crawl target, and anchor text is presentation metadata rather
    than part of the link value itself.
    """
    parser = _LinkParser(base_url=base_url)
    parser.feed(html)
    parser.close()
    return parser.links()


def text_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_warc(
    *,
    final_url: str,
    status_code: int,
    headers: Mapping[str, str],
    html_bytes: bytes,
    resources: list[BrowserUrlResource],
) -> bytes:
    records = [
        _warc_response_record(
            url=final_url,
            status_code=status_code,
            headers=headers,
            body=html_bytes,
        )
    ]
    for resource in resources[:MAX_BROWSER_CAPTURE_REQUESTS]:
        if not resource.body:
            continue
        records.append(
            _warc_response_record(
                url=resource.url,
                status_code=resource.status_code or 0,
                headers=resource.headers,
                body=resource.body,
            )
        )
    return b"".join(records)


def _error_result(
    input_url: str,
    error: str,
    *,
    reason: str,
    final_url: str | None = None,
    status_code: int | None = None,
    byte_count: int = 0,
    network_metadata: dict[str, Any] | None = None,
    extractor_version: str | None = None,
    render_mode: str = "static",
) -> StaticUrlCaptureResult:
    return StaticUrlCaptureResult(
        status="error",
        input_url=input_url,
        final_url=final_url,
        status_code=status_code,
        byte_count=byte_count,
        network_metadata=network_metadata or {},
        error=error,
        reason=reason,
        extractor_version=extractor_version,
        render_mode=render_mode,
    )


def _browser_resource(
    item: BrowserUrlResource | Mapping[str, Any],
) -> BrowserUrlResource:
    if isinstance(item, BrowserUrlResource):
        return item
    body = item.get("body")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes):
        body = None
    headers = item.get("headers")
    return BrowserUrlResource(
        url=str(item.get("url") or ""),
        method=str(item.get("method") or "GET"),
        status_code=item.get("status_code")
        if isinstance(item.get("status_code"), int)
        else None,
        headers=headers if isinstance(headers, Mapping) else {},
        body=body,
        elapsed_ms=item.get("elapsed_ms")
        if isinstance(item.get("elapsed_ms"), int)
        else None,
    )


def _browser_network_metadata(
    *,
    input_url: str,
    final_url: str,
    status_code: int,
    content_type: str | None,
    html_byte_count: int,
    screenshot_byte_count: int,
    aggregate_byte_count: int,
    elapsed_ms: int | None,
    resources: list[BrowserUrlResource],
    include_warc: bool,
) -> dict[str, Any]:
    parsed = urlparse(final_url)
    resource_facts = []
    for resource in resources[:MAX_BROWSER_CAPTURE_REQUESTS]:
        resource_facts.append(
            {
                "method": resource.method,
                "url_hash": text_hash(resource.url),
                "host": urlparse(resource.url).netloc,
                "status_code": resource.status_code,
                "content_type": _content_type(resource.headers),
                "byte_count": len(resource.body or b""),
                "elapsed_ms": resource.elapsed_ms,
            }
        )
    return {
        "method": "GET",
        "render_mode": "playwright",
        "host": parsed.netloc,
        "url_hash": text_hash(input_url),
        "final_url_hash": text_hash(final_url),
        "status_code": status_code,
        "content_type": content_type,
        "byte_count": html_byte_count,
        "html_byte_count": html_byte_count,
        "screenshot_byte_count": screenshot_byte_count,
        "aggregate_byte_count": aggregate_byte_count,
        "elapsed_ms": elapsed_ms,
        "request_count": max(1, len(resources)),
        "resource_count": len(resources),
        "resources": resource_facts,
        "include_warc": include_warc,
    }


def _warc_response_record(
    *,
    url: str,
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> bytes:
    http_headers = _sanitized_http_headers(headers)
    http_message = (
        f"HTTP/1.1 {status_code} {_http_reason(status_code)}\r\n".encode("utf-8")
        + http_headers
        + b"\r\n"
        + body
    )
    digest = hashlib.sha256(body).hexdigest()
    warc_headers = (
        "WARC/1.0\r\n"
        "WARC-Type: response\r\n"
        f"WARC-Record-ID: <urn:uuid:{uuid.uuid4()}>\r\n"
        f"WARC-Target-URI: {url}\r\n"
        f"WARC-Date: {_utc_now()}\r\n"
        "Content-Type: application/http; msgtype=response\r\n"
        f"Content-Length: {len(http_message)}\r\n"
        f"WARC-Payload-Digest: sha256:{digest}\r\n"
        "\r\n"
    ).encode("utf-8")
    return warc_headers + http_message + b"\r\n\r\n"


def _sanitized_http_headers(headers: Mapping[str, str]) -> bytes:
    blocked = {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
    }
    lines: list[bytes] = []
    for key, value in headers.items():
        lower = key.lower()
        if lower in blocked or "token" in lower or "secret" in lower:
            continue
        if lower in {"content-type", "content-language", "last-modified", "etag"}:
            safe_key = re.sub(r"[^A-Za-z0-9-]", "", key)
            safe_value = str(value).replace("\r", " ").replace("\n", " ")
            lines.append(f"{safe_key}: {safe_value}\r\n".encode("utf-8"))
    return b"".join(lines)


def _http_reason(status_code: int) -> str:
    return {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        304: "Not Modified",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        500: "Internal Server Error",
    }.get(status_code, "Status")


def _warc_record_count(warc_bytes: bytes) -> int:
    return warc_bytes.count(b"WARC/1.0\r\n")


def _network_metadata(
    *,
    input_url: str,
    final_url: str,
    status_code: int,
    content_type: str | None,
    byte_count: int,
    elapsed_ms: int | None,
    redirects: list[dict[str, Any]],
) -> dict[str, Any]:
    parsed = urlparse(final_url)
    return {
        "method": "GET",
        "render_mode": "static",
        "host": parsed.netloc,
        "url_hash": text_hash(input_url),
        "final_url_hash": text_hash(final_url),
        "status_code": status_code,
        "content_type": content_type,
        "byte_count": byte_count,
        "elapsed_ms": elapsed_ms,
        "redirects": list(redirects),
    }


def _content_type(headers: Mapping[str, str]) -> str | None:
    value = ""
    for key, header_value in headers.items():
        if key.lower() == "content-type":
            value = header_value
            break
    content_type = value.split(";", 1)[0].strip().lower()
    return content_type or None


def _decode_html(data: bytes, headers: Mapping[str, str]) -> str:
    charset = None
    content_type = next(
        (value for key, value in headers.items() if key.lower() == "content-type"),
        "",
    )
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type)
    if match:
        charset = match.group(1)
    return data.decode(charset or "utf-8", errors="replace")


def _trafilatura_version() -> str | None:
    try:
        return version("trafilatura")
    except PackageNotFoundError:
        return None


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _published_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned[:10]
    return cleaned


class _MetadataParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._in_title = False
        self._title_parts: list[str] = []
        self._canonical_url: str | None = None
        self._author: str | None = None
        self._published_at: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): (value or "") for key, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
            return
        if tag.lower() == "link":
            rels = {part.strip().lower() for part in attr.get("rel", "").split()}
            href = attr.get("href")
            if "canonical" in rels and href and self._canonical_url is None:
                self._canonical_url = urljoin(self._base_url, href.strip())
            return
        if tag.lower() != "meta":
            return
        key = (attr.get("name") or attr.get("property") or "").strip().lower()
        value = (attr.get("content") or "").strip()
        if not key or not value:
            return
        if key in {"author", "article:author", "parsely-author"}:
            self._author = self._author or value
        if key in {
            "article:published_time",
            "article:published",
            "published_time",
            "date",
            "dc.date",
            "dc.date.issued",
        }:
            self._published_at = self._published_at or _published_value(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)

    def metadata(self) -> dict[str, str]:
        title = " ".join(part.strip() for part in self._title_parts if part.strip())
        metadata: dict[str, str] = {}
        if self._canonical_url:
            metadata["canonical_url"] = self._canonical_url
        if title:
            metadata["title"] = re.sub(r"\s+", " ", title).strip()
        if self._author:
            metadata["author"] = self._author
        if self._published_at:
            metadata["published_at"] = self._published_at
        return metadata


class _LinkParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._document_url = base_url
        self._base_url: str | None = None
        self._active_href: str | None = None
        self._active_text: list[str] = []
        self._raw_links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): (value or "") for key, value in attrs}
        lowered = tag.lower()
        if lowered == "base" and attr.get("href") and self._base_url is None:
            candidate = urljoin(self._document_url, attr["href"].strip())
            parsed = urlparse(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                self._base_url = candidate
            return
        if lowered != "a":
            return
        self._finish_anchor()
        self._active_href = attr.get("href", "").strip() or None
        self._active_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a":
            self._finish_anchor()

    def handle_data(self, data: str) -> None:
        if self._active_href is not None:
            self._active_text.append(data)

    def close(self) -> None:
        self._finish_anchor()
        super().close()

    def links(self) -> list[dict[str, str]]:
        base_url = self._base_url or self._document_url
        links: list[dict[str, str]] = []
        seen: set[str] = set()
        for href, text in self._raw_links:
            absolute, _fragment = urldefrag(urljoin(base_url, href))
            parsed = urlparse(absolute)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or absolute in seen
            ):
                continue
            seen.add(absolute)
            links.append({"url": absolute, "anchor_text": text})
        return links

    def _finish_anchor(self) -> None:
        href = self._active_href
        text = re.sub(r"\s+", " ", " ".join(self._active_text)).strip()
        self._active_href = None
        self._active_text = []
        if not href:
            return
        self._raw_links.append((href, text))


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()
