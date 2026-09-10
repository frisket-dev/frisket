"""Key broker: op processes request model completions over a broker endpoint
(a Unix domain socket on POSIX; an authenticated loopback TCP socket on
native Windows, since ``asyncio.start_unix_server``/``AF_UNIX`` are POSIX-only).
Provider keys never enter the op's process. Because
the Windows transport is a loopback TCP port rather than a filesystem-scoped
socket, every request additionally carries a per-broker capability token
(``secrets.token_urlsafe(32)``, checked with ``secrets.compare_digest``) so
reachability alone is never sufficient authorization on either transport.

Protocol: one JSON object per line.
  request:  {"model": ..., "messages": [...], "schema": ...?, "max_tokens": ...?,
             "repair_attempts": ...?, "mechanism": ...?, "token": "..."}
  response: {"ok": true, "response": {<LLMResponse fields>}}
          | {"ok": false, "error": "...", "status": 401}

Structured (schema-bearing) traffic: ``_handle`` routes it
through :class:`frisket.llm.structured.StructuredCompleter` — same
capability-resolution + jsonschema validation + bounded repair every other
migrated caller gets, not a parallel hand-rolled path. This runs entirely on
the TRUSTED broker-process side of the socket: the in-process shim/Agent cannot
cross the socket, so the sandboxed OP PROCESS never runs pydantic-ai,
unaffected by adoption; it still only speaks this raw line protocol.
``repair_attempts`` is CLAMPED server-side (:func:`_clamp_repair_attempts`)
because, unlike every other completer caller (a cooperating piece of core
code), op code behind the broker is UNTRUSTED — it cannot buy unbounded
repair cost by sending a large number over the socket. A structurally
invalid op-supplied ``schema`` raises ``jsonschema.exceptions.SchemaError``,
which the completer wraps to :class:`SchemaViolation` uniformly (structured.py)
so it lands in the ``except LLMError`` branch below instead of crashing the
connection handler. Regex-DoS via ``pattern``/``patternProperties`` in an
untrusted schema is an ACCEPTED RESIDUAL risk — not solved
here; would need a pattern-complexity linter or a regex-execution timeout,
its own future mint. Schemaless traffic (``schema`` omitted or ``null``,
e.g. a free-text op prompt) is UNCHANGED — it stays on ``router.complete``
directly, exactly as the schemaless core callers in
``executor/action_families/reduces.py`` do by design.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote

from frisket.ai.model_defaults import DEFAULT_MAX_OUTPUT_TOKENS
from frisket.ai.llm import LLMError, LLMRequest, ModelRouter
from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest

# Hard ceiling on a broker-supplied repair_attempts — op code is
# untrusted, unlike every other StructuredCompleter caller.
BROKER_MAX_REPAIR_ATTEMPTS = 3


def _clamp_repair_attempts(requested: object) -> int:
    """Untrusted op-supplied ``repair_attempts``, clamped server-side to a
    hard max — never caller-trusted the way every other
    (cooperating, core-code) completer caller's ``repair_attempts`` is."""
    try:
        n = int(requested) if requested is not None else 1
    except (TypeError, ValueError):
        n = 1
    return max(0, min(n, BROKER_MAX_REPAIR_ATTEMPTS))


class KeyBroker:
    def __init__(self, router: ModelRouter, socket_path: str | Path):
        self.router = router
        self.structured = StructuredCompleter(router)
        # POSIX requires and uses this path for the Unix domain socket.
        # Windows accepts it for call-site uniformity but neither creates
        # nor unlinks it — the Windows transport is loopback TCP. The
        # promised unix:// wire form is always absolute; normalize a
        # relative path here rather than emit an endpoint whose
        # urlsplit(...).path a consumer would misparse (a bare relative
        # filename becomes a URL *netloc* with an empty path).
        self.socket_path = str(Path(socket_path).resolve())
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._endpoint: str | None = None

    @property
    def endpoint(self) -> str:
        """The wire endpoint a sandboxed op connects to, in the form
        ``unix://<absolute-path>?token=<token>`` on POSIX or
        ``tcp://127.0.0.1:<assigned-port>?token=<token>`` on Windows. Raises
        RuntimeError if accessed before a successful `start()` rather than
        returning a partial value."""
        if self._endpoint is None:
            raise RuntimeError(
                "KeyBroker.endpoint accessed before a successful start()"
            )
        return self._endpoint

    async def start(self) -> None:
        if os.name == "nt":
            # asyncio.start_unix_server/AF_UNIX are Unix-only; use an
            # authenticated loopback TCP endpoint on Windows instead. Never
            # bind 0.0.0.0, ::, or a fixed port.
            self._server = await asyncio.start_server(
                self._handle, host="127.0.0.1", port=0
            )
            sockets = self._server.sockets or ()
            if not sockets:
                raise RuntimeError("KeyBroker failed to bind a loopback TCP socket")
            port = sockets[0].getsockname()[1]
            self._endpoint = f"tcp://127.0.0.1:{port}?token={self._token}"
        else:
            self._server = await asyncio.start_unix_server(
                self._handle, path=self.socket_path
            )
            encoded_path = quote(self.socket_path, safe="/")
            self._endpoint = f"unix://{encoded_path}?token={self._token}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.name != "nt":
            Path(self.socket_path).unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as e:
                out = {"ok": False, "error": f"bad request: {e}", "status": 400}
                writer.write((json.dumps(out) + "\n").encode())
                await writer.drain()
                return
            token = payload.get("token")
            if not isinstance(token, str) or not secrets.compare_digest(
                token, self._token
            ):
                # Missing/incorrect token: one JSON 401, no model work.
                out = {"ok": False, "error": "unauthorized", "status": 401}
                writer.write((json.dumps(out) + "\n").encode())
                await writer.drain()
                return
            try:
                schema = payload.get("schema")
                if schema is not None:
                    # Schema-bearing: the completer's full contract.
                    sreq = StructuredRequest(
                        model=payload["model"],
                        messages=payload["messages"],
                        schema=schema,
                        method=payload.get("mechanism") or "auto",
                        repair_attempts=_clamp_repair_attempts(
                            payload.get("repair_attempts")
                        ),
                        max_tokens=payload.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                        temperature=payload.get("temperature", 0.0),
                    )
                    result = await self.structured.complete(sreq)
                    resp = result.response
                else:
                    req = LLMRequest(
                        model=payload["model"],
                        messages=payload["messages"],
                        schema=None,
                        max_tokens=payload.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                        temperature=payload.get("temperature", 0.0),
                    )
                    resp = await self.router.complete(req)
                out = {"ok": True, "response": asdict(resp)}
            except LLMError as e:
                out = {"ok": False, "error": str(e), "status": e.status}
            except KeyError as e:
                out = {"ok": False, "error": f"bad request: {e}", "status": 400}
            writer.write((json.dumps(out) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()


# Client helper, importable inside sandboxed op code (stdlib only). Parses
# FRISKET_BROKER_ENDPOINT (unix://<path>?token=... or
# tcp://127.0.0.1:<port>?token=...) and connects over the matching transport
# -- AF_UNIX on POSIX, AF_INET to the literal (host, port) tuple on Windows
# (no DNS lookup: the sandbox audit hook denies name-resolution events).
CLIENT_SNIPPET = (
    '''
import json, os, socket
from urllib.parse import parse_qs, unquote, urlsplit

def frisket_complete(model, messages, schema=None, max_tokens=%d,
                      repair_attempts=1, mechanism=None):
    """Call a model via the frisket key broker. No keys in this process.
    repair_attempts/mechanism only matter when `schema` is set -- the broker
    routes schema-bearing requests through the StructuredCompleter and clamps
    repair_attempts server-side (an op cannot buy unbounded repair cost)."""
    endpoint = os.environ["FRISKET_BROKER_ENDPOINT"]
    parts = urlsplit(endpoint)
    token = parse_qs(parts.query).get("token", [None])[0]
    if parts.scheme == "unix":
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(unquote(parts.path))
    elif parts.scheme == "tcp":
        if parts.hostname != "127.0.0.1":
            raise RuntimeError(
                "broker endpoint tcp host must be 127.0.0.1, got "
                + repr(parts.hostname)
            )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", int(parts.port)))
    else:
        raise RuntimeError("unsupported broker endpoint scheme: " + parts.scheme)
    s.sendall((json.dumps({"model": model, "messages": messages,
                           "schema": schema, "max_tokens": max_tokens,
                           "repair_attempts": repair_attempts,
                           "mechanism": mechanism, "token": token}) + "\\n").encode())
    buf = b""
    while not buf.endswith(b"\\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    out = json.loads(buf)
    if not out.get("ok"):
        raise RuntimeError(out.get("error"))
    return out["response"]
'''
    % DEFAULT_MAX_OUTPUT_TOKENS
)
