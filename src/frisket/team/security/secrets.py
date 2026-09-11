"""Envelope encryption for secrets at rest (org-supplied BYO API keys,
project-scoped provider keys, plugin env vars). This open-core module contains
no tenant or billing policy.

Threat model unchanged from the pre-cutover design: the control-plane DB or
a `.frisket` project bundle may leak (backup, snapshot, unrelated SQL
injection). A leaked ciphertext column must not yield plaintext without also
holding the master key, which lives only in the environment / a locally
persisted key file, never in the database and never in this repo.

Envelope scheme (v2, this cutover):

  * AEAD: AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`),
    a reviewed construction from a vetted library — not hand-rolled crypto.
  * KDF: HKDF-SHA256 derives a fresh 256-bit key per record from the master
    key material and a random per-record salt, so no derived key is reused
    across secrets even under the same master key.
  * A fresh random 96-bit nonce is drawn per encryption.

v1 (the pre-cutover custom SHA-256-keystream+HMAC scheme) is retired with no
compatibility reader: `decrypt_secret` raises a legible error naming the
reset/re-enter path for anything that isn't a v2 envelope. Existing v1
ciphertext was pre-release development/demo state and is intentionally
disposable.

Master key resolution — NEVER a shared in-repo default:

  * If `FRISKET_SECRETS_MASTER_KEY` is set, its bytes are the key material
    fed into HKDF (matches the historical `_master_key()` contract: an
    operator-chosen passphrase, not raw key bytes).
  * Otherwise a random 256-bit key is generated on first use and persisted
    to a local file (see `_generated_key_path`) with owner-only permissions,
    so a restart still decrypts. Every unconfigured install gets its OWN
    generated key — never a constant shared across installs. This is
    adequate for a single-node self-host; a horizontally-scaled or
    multi-replica deployment MUST set FRISKET_SECRETS_MASTER_KEY explicitly
    (each replica would otherwise generate its own, mutually incompatible,
    key).
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from secrets import token_bytes, token_hex

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from filelock import FileLock

ENVELOPE_VERSION = 2
ALGORITHM = "aes-256-gcm"

_DERIVED_KEY_LEN = 32
_SALT_LEN = 16
_NONCE_LEN = 12
_HKDF_INFO = b"frisket.security.secrets.v2"


def using_default_master_key() -> bool:
    """True when FRISKET_SECRETS_MASTER_KEY is unset, so encrypt/decrypt fall
    back to a locally generated + persisted per-instance key rather than an
    operator-chosen one; there is never a shared in-repo default. Pure
    env read, no side effects (no file I/O) — safe to call from health
    checks / startup posture warnings without generating anything."""
    return not bool(os.environ.get("FRISKET_SECRETS_MASTER_KEY"))


def _generated_key_path() -> Path:
    """Where a locally generated master key is persisted when the operator
    has not set FRISKET_SECRETS_MASTER_KEY. Checked in order of
    specificity; the first configured location wins. Falls back to a
    per-user dotfile so a bare `frisket` CLI invocation works unconfigured."""
    env = os.environ
    for var, suffix in (
        ("FRISKET_SECRETS_KEY_FILE", None),
        ("FRISKET_SECRETS_KEY_DIR", "master.key"),
        ("FRISKET_DATA_DIR", "secrets/master.key"),
        ("FRISKET_HOME", "secrets/master.key"),
        ("XDG_STATE_HOME", "frisket/secrets/master.key"),
        ("XDG_DATA_HOME", "frisket/secrets/master.key"),
    ):
        value = env.get(var, "").strip()
        if not value:
            continue
        return Path(value) if suffix is None else Path(value) / suffix
    return Path.home() / ".frisket" / "secrets" / "master.key"


def _load_or_create_master_key() -> bytes:
    """Raw key material: the operator's explicit passphrase encoded as UTF-8
    bytes, or a randomly generated key persisted to disk on first use.
    Never a shared in-repo constant."""
    explicit = os.environ.get("FRISKET_SECRETS_MASTER_KEY", "")
    if explicit:
        return explicit.encode("utf-8")

    path = _generated_key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = path.read_bytes()
        if data:
            return data

    key = token_bytes(32)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{token_hex(4)}")
    tmp_path.write_bytes(key)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass  # best-effort on platforms without POSIX permission bits
    tmp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _derive_key(master_key: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_LEN,
        salt=salt,
        info=_HKDF_INFO,
    ).derive(master_key)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data)


def encrypt_secret(plaintext: str, *, master_key: bytes | None = None) -> str:
    """Envelope-encrypt a secret with AES-256-GCM. Returns an opaque JSON
    string for storage. A fresh HKDF-derived key (via a random per-record
    salt) and a fresh random nonce are used every call, so no plaintext ever
    produces the same envelope twice."""
    master_key = master_key or _load_or_create_master_key()
    salt = token_bytes(_SALT_LEN)
    derived_key = _derive_key(master_key, salt)
    nonce = token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(derived_key).encrypt(nonce, plaintext.encode("utf-8"), None)
    envelope = {
        "v": ENVELOPE_VERSION,
        "alg": ALGORITHM,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ct": _b64(ciphertext),
    }
    return json.dumps(envelope, separators=(",", ":"))


def decrypt_secret(blob: str, *, master_key: bytes | None = None) -> str:
    """Reverse of encrypt_secret. Raises ValueError on tamper/wrong key, and
    a distinct legible ValueError for a pre-cutover v1 envelope — there is
    no compatibility reader because pre-release ciphertext is disposable;
    reset/re-enter the secret instead."""
    envelope = json.loads(blob)
    version = envelope.get("v")
    if version != ENVELOPE_VERSION:
        raise ValueError(
            f"unsupported legacy secret envelope (v{version!r}); the AEAD "
            "clean cutover retired the old format with no compatibility "
            "reader — reset/re-enter this secret"
        )
    master_key = master_key or _load_or_create_master_key()
    salt = _b64d(envelope["salt"])
    nonce = _b64d(envelope["nonce"])
    ciphertext = _b64d(envelope["ct"])
    derived_key = _derive_key(master_key, salt)
    try:
        plaintext = AESGCM(derived_key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise ValueError(
            "secret authentication failed (tampered ciphertext or wrong master key)"
        ) from exc
    return plaintext.decode("utf-8")


def key_hint(plaintext: str) -> str:
    """A non-reversible display hint (last 4 chars) so the UI can show which
    key is stored without ever echoing the secret back."""
    tail = plaintext[-4:] if len(plaintext) > 4 else ""
    return f"...{tail}" if tail else "..."


# Windows-only cross-process read-after-write visibility lag: root-caused
# via a native windows-latest CI failure where the losing (reading) process
# entered this function's FileLock-guarded section strictly AFTER the
# winning (writing) process had returned from its own os.write + os.fsync +
# os.close + FileLock release, yet path.read_bytes() still observed fewer
# than 32 bytes. Both processes are correctly serialized by FileLock (the
# reader cannot even evaluate path.exists() until the writer's critical
# section, including the lock release, has fully completed) -- this is not
# a same-process or double-creation race, and the writer's own returncode
# was 0 (it wrote and read back 32 bytes successfully in-process). The most
# plausible explanation is a Windows filesystem/AV-interposition visibility
# gap (e.g. Windows Defender's on-access scanner interposing on the
# freshly created file) between another process's completed, fsynced write
# and this process's subsequent read seeing it -- NTFS's cache manager is
# normally unified across handles/processes on one machine, so this should
# be rare, and empirically it is (intermittent, not reproduced on most
# runs). _read_persisted_key retries a short, bounded number of times on
# Windows before failing loud, which is safe here: this call always runs
# already holding the exclusive FileLock, so a short-lived visibility gap
# is the only thing a retry can paper over -- a genuinely corrupt/truncated
# file still raises after the retry budget is exhausted.
_WINDOWS_KEY_READ_RETRY_ATTEMPTS = 5
_WINDOWS_KEY_READ_RETRY_BASE_SECONDS = 0.05


def _read_persisted_key(
    path: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    attempts = _WINDOWS_KEY_READ_RETRY_ATTEMPTS if os.name == "nt" else 1
    key = b""
    for attempt in range(attempts):
        key = path.read_bytes()
        if len(key) == 32:
            return key
        if attempt + 1 < attempts:
            sleep(_WINDOWS_KEY_READ_RETRY_BASE_SECONDS * (attempt + 1))
    raise ValueError(f"invalid secret key file {path}: expected 32 bytes")


def load_or_create_secret_key(path: str | Path) -> bytes:
    """Atomically load/create a node-local key with process-safe file locking."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(str(lock_path), timeout=-1):
        if path.exists():
            return _read_persisted_key(path)
        key = token_bytes(32)
        tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{token_hex(4)}")
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            written = os.write(fd, key)
            if written != len(key):
                raise OSError(
                    f"short write persisting secret key to {path}: "
                    f"wrote {written} of {len(key)} bytes"
                )
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp_path.replace(path)
        if os.name != "nt":
            # Extra POSIX directory-entry durability; opening a directory
            # this way is not a portable Windows operation.
            # Note: this branch is only reached from the just-created path
            # above -- the read path (path.exists() True) always returns
            # before this point, on every platform, so it never needs its
            # own guard here.
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return key
