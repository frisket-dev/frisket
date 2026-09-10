"""The server-level media egress proxy setting.

One operator-owned value: a proxy URL (usually ``socks5h://127.0.0.1:1080``)
that media downloads egress through instead of the server's own network.
The intended shape is a reverse tunnel the operator runs FROM their own
machine (``frisket proxy up``), making the server's loopback a SOCKS5 proxy
whose traffic exits through the operator's connection — which is why
loopback hosts are the strongly preferred form and anything else only
warns. The setting is transport-agnostic: it holds a proxy URL and nothing
about how the tunnel behind it was built.

Two sources, admin-set state winning over boot-time env:

* the state file ``media_proxy.json`` under the server data dir
  (``FRISKET_DATA_DIR``, else ``DEFAULT_DATA_DIR`` — one resolution, shared
  by the admin surface that writes it and the transport that reads it),
  written by the operator admin surface at runtime;
* the ``FRISKET_MEDIA_PROXY`` environment variable, the boot-time form for
  deployments that configure everything through env.

This module is ops-layer on purpose: the download path
(``frisket.ops.ytdlp``) reads it directly, and the server/team admin
surfaces call down into it — never the other way around. The proxy URL is
operator config, not a secret (URLs with embedded credentials are refused),
but it still never reaches receipts: download provenance records only the
boolean fact that a proxy was in use.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MEDIA_PROXY_ENV = "FRISKET_MEDIA_PROXY"
DATA_DIR_ENV = "FRISKET_DATA_DIR"
# The server data dir when ``FRISKET_DATA_DIR`` is unset. This MUST stay equal
# to ``frisket.team.config.team_config_from_env``'s own default for the same
# variable: that function's ``config.data_dir`` is what the admin surface
# writes ``media_proxy.json`` through, and ``resolve_media_proxy()`` below is
# what the download transport reads it back with. The ops layer may not import
# the team layer (scripts/ci/import_boundaries.json,
# "ops-does-not-import-server-or-team"), so the two are held equal by
# ``tests/ops/test_media_proxy.py``'s parity test rather than by derivation --
# that test goes red the moment either default moves.
DEFAULT_DATA_DIR = "./frisket-data"
MEDIA_PROXY_STATE_FILENAME = "media_proxy.json"

# socks5h resolves hostnames on the proxy side (the tunnel's far end), which
# is what an anti-bot workaround usually wants; socks5 and plain http stay
# accepted for operators with an existing local proxy of either kind.
MEDIA_PROXY_ALLOWED_SCHEMES = ("socks5", "socks5h", "http")

# A zero-body connectivity endpoint on the provider this feature exists for:
# reaching it through the proxy proves the tunnel egresses end-to-end.
MEDIA_PROXY_PROBE_URL = "https://www.youtube.com/generate_204"
MEDIA_PROXY_PROBE_TIMEOUT_SECONDS = 10.0

_LOOPBACK_NAMES = frozenset({"localhost"})


@dataclass(frozen=True)
class MediaProxyValidation:
    """A validated proxy URL plus non-fatal warnings about its shape."""

    url: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaProxyConfig:
    """The resolved setting: the URL and which source supplied it.

    ``env_invalid`` carries the rejection reason when the env var is set but
    unusable — the one misconfiguration that would otherwise be invisible
    (status surfaces would just say "no proxy is set")."""

    url: str | None
    source: str | None  # 'admin' (state file) | 'env' | None
    env_invalid: str | None = None


@dataclass(frozen=True)
class MediaProxyProbeResult:
    """Outcome of one server-side probe request through the proxy."""

    ok: bool
    probe_url: str
    status_code: int | None = None
    elapsed_ms: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object | None]:
        return {
            "ok": self.ok,
            "probe_url": self.probe_url,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


def _host_is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_media_proxy_url(url: str) -> MediaProxyValidation:
    """Validate one proxy URL; raise ``ValueError`` on a shape that can never
    work, return warnings for shapes that work but are discouraged.

    Refusals: non-socks5/socks5h/http schemes, a missing host, an invalid
    port, embedded credentials (a URL with a password is a secret, and this
    setting is deliberately plain config — it is echoed back to the
    operator), and path/query/fragment noise. Non-loopback hosts only warn:
    the normal deployment points at the server's own loopback where the
    operator's tunnel listens, but a proxy elsewhere on the operator's
    network is their call.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("proxy URL must not be empty")
    parts = urlsplit(candidate)
    if parts.scheme not in MEDIA_PROXY_ALLOWED_SCHEMES:
        allowed = ", ".join(MEDIA_PROXY_ALLOWED_SCHEMES)
        raise ValueError(f"proxy URL scheme must be one of: {allowed}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(
            "proxy URL must not embed credentials; this setting is plain "
            "config and is echoed back to operators"
        )
    try:
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"proxy URL is invalid: {exc}") from exc
    if not host:
        raise ValueError("proxy URL must include a host")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("proxy URL must be scheme://host[:port] with no path")
    warnings: list[str] = []
    if not _host_is_loopback(host):
        warnings.append(
            f"proxy host {host!r} is not loopback; the recommended setup is a "
            "tunnel listening on the server's own 127.0.0.1 so no third "
            "machine sits in the download path"
        )
    if port is None:
        warnings.append("proxy URL has no explicit port; 1080 is the usual choice")
    return MediaProxyValidation(url=candidate, warnings=tuple(warnings))


def container_gateway(root: str | Path = "/") -> str | None:
    """The default-route gateway as seen from inside a container, or None
    when this process is not containerized or the gateway is undetectable.

    Exists for the media proxy's one deployment reality: in the standard
    Docker install the server's ``127.0.0.1`` is the *container's* loopback,
    so an operator tunnel landing on the HOST loopback is unreachable — the
    bridge gateway address is the container's route to the host, and the
    tunnel must bind there instead. Detection is deliberately conservative:
    without a container marker the answer is None, because on bare metal the
    default gateway is the LAN router and must never become a proxy bind."""
    base = Path(root)
    if not _looks_containerized(base):
        return None
    try:
        lines = (base / "proc/net/route").read_text().splitlines()
    except OSError:
        return None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000" or fields[2] == "00000000":
            continue
        try:
            packed = bytes.fromhex(fields[2])
        except ValueError:
            continue
        if len(packed) == 4:
            # /proc/net/route stores the gateway as a little-endian u32.
            return str(ipaddress.IPv4Address(bytes(reversed(packed))))
    return None


def _looks_containerized(base: Path) -> bool:
    if (base / ".dockerenv").exists():
        return True
    try:
        cgroup = (base / "proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(
        marker in cgroup for marker in ("docker", "containerd", "kubepods", "libpod")
    )


def media_proxy_state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / MEDIA_PROXY_STATE_FILENAME


def _state_dir(data_dir: str | Path | None, env: dict[str, str]) -> Path:
    """Where ``media_proxy.json`` lives — the SAME resolution on both sides.

    A caller that holds the server config passes its ``data_dir`` (the team
    admin routes and the hosted org-network service both do). A caller that
    does not — the download transport, running in the worker process — gets
    the env var, and, when that is unset, the same ``./frisket-data`` default
    ``team_config_from_env`` applies.

    That last clause is the fix for a two-surfaces-two-answers bug: this used
    to return ``None`` with no env var, so on a bare
    ``uvicorn --factory frisket.team.app:create_team_app_from_env
    --no-proxy-headers`` deployment (neither the CLI, which
    exports ``FRISKET_DATA_DIR``, nor Compose, which pins it) the operator
    could set a proxy, see ``GET /api/admin/media-proxy`` report it
    configured, watch ``POST /api/admin/media-proxy/check`` probe
    SUCCESSFULLY through the tunnel — and then have every yt-dlp download
    egress from the server's real IP, because ``ops/ytdlp.py`` read nothing
    here and omitted ``--proxy``. The serialization cap in
    ``sdk/ops/ytdlp_download.py`` read nothing here either, so those
    downloads also ran in parallel: the exact anti-bot signature the proxy
    exists to avoid.
    """
    if data_dir is not None:
        return Path(data_dir)
    raw = (env.get(DATA_DIR_ENV) or "").strip()
    return Path(raw or DEFAULT_DATA_DIR)


def read_media_proxy(
    data_dir: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> MediaProxyConfig:
    """Resolve the effective proxy setting.

    The admin-written state file wins over the env var: the file is the most
    recent explicit operator action (``frisket proxy up``), while env is the
    boot-time default. A stored value that no longer validates resolves to
    None rather than steering downloads through garbage — the admin surface
    validates on write, so this only guards external file edits.
    """
    resolved_env = dict(os.environ if env is None else env)
    stored = _read_state_url(media_proxy_state_path(_state_dir(data_dir, resolved_env)))
    if stored is not None:
        return MediaProxyConfig(url=stored, source="admin")
    raw = (resolved_env.get(MEDIA_PROXY_ENV) or "").strip()
    if raw:
        try:
            validated = validate_media_proxy_url(raw)
        except ValueError as exc:
            return MediaProxyConfig(url=None, source=None, env_invalid=str(exc))
        return MediaProxyConfig(url=validated.url, source="env")
    return MediaProxyConfig(url=None, source=None)


def _read_state_url(path: Path) -> str | None:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("url")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return validate_media_proxy_url(raw).url
    except ValueError:
        return None


def write_media_proxy(data_dir: str | Path, url: str | None) -> MediaProxyValidation:
    """Persist (or with ``url=None`` clear) the admin-set proxy URL.

    Validates before writing and writes atomically so a concurrent reader
    never sees a torn file. Returns the validation (with warnings) so the
    admin surface can relay them.
    """
    path = media_proxy_state_path(data_dir)
    if url is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return MediaProxyValidation(url="")
    validated = validate_media_proxy_url(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps({"url": validated.url}))
    os.replace(tmp, path)
    return validated


def resolve_media_proxy() -> str | None:
    """The download path's one-call resolution of the effective proxy URL."""
    return read_media_proxy().url


def _httpx_proxy_url(url: str) -> str:
    # httpx spells remote-resolution SOCKS as plain socks5://; its transport
    # already sends the hostname to the proxy, so socks5h maps down safely.
    if url.startswith("socks5h://"):
        return "socks5://" + url[len("socks5h://") :]
    return url


def probe_media_proxy(
    proxy_url: str,
    *,
    probe_url: str = MEDIA_PROXY_PROBE_URL,
    timeout: float = MEDIA_PROXY_PROBE_TIMEOUT_SECONDS,
) -> MediaProxyProbeResult:
    """One tiny read-only request through the proxy, reporting reachability.

    Fetches a zero-body connectivity endpoint; any HTTP status proves the
    proxy carried the request end-to-end, so only transport errors fail the
    probe. Never raises — the result carries the error text instead, bounded
    so a hostile upstream cannot bloat the admin response.
    """
    started = time.monotonic()
    try:
        response = httpx.get(
            probe_url,
            proxy=_httpx_proxy_url(proxy_url),
            timeout=timeout,
            follow_redirects=False,
        )
    except Exception as exc:
        return MediaProxyProbeResult(
            ok=False,
            probe_url=probe_url,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=str(exc)[:500] or exc.__class__.__name__,
        )
    return MediaProxyProbeResult(
        ok=True,
        probe_url=probe_url,
        status_code=response.status_code,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# One-entry probe cache so a polling status surface does not turn into a
# stream of outbound requests through the operator's tunnel.
_PROBE_CACHE_TTL_SECONDS = 5.0
_probe_cache: dict[str, tuple[float, MediaProxyProbeResult]] = {}


def probe_media_proxy_cached(
    proxy_url: str,
    *,
    ttl: float = _PROBE_CACHE_TTL_SECONDS,
) -> MediaProxyProbeResult:
    """``probe_media_proxy`` behind a per-URL TTL cache for polling callers."""
    now = time.monotonic()
    cached = _probe_cache.get(proxy_url)
    if cached is not None and now - cached[0] < ttl:
        return cached[1]
    result = probe_media_proxy(proxy_url)
    _probe_cache.clear()
    _probe_cache[proxy_url] = (time.monotonic(), result)
    return result


__all__ = [
    "DATA_DIR_ENV",
    "DEFAULT_DATA_DIR",
    "MEDIA_PROXY_ALLOWED_SCHEMES",
    "MEDIA_PROXY_ENV",
    "MEDIA_PROXY_PROBE_URL",
    "MEDIA_PROXY_STATE_FILENAME",
    "MediaProxyConfig",
    "MediaProxyProbeResult",
    "MediaProxyValidation",
    "container_gateway",
    "media_proxy_state_path",
    "probe_media_proxy",
    "probe_media_proxy_cached",
    "read_media_proxy",
    "resolve_media_proxy",
    "validate_media_proxy_url",
    "write_media_proxy",
]
