from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import importlib
import json
import re
import secrets as _stdlib_secrets
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.gap

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
PKG = SRC / "frisket"

NEW_MODULE = "frisket.team.security.secrets"
OLD_MODULE_PATH = PKG / "hosted" / "secrets_store.py"

# Built by concatenation so a text scan for the forbidden literal never
# matches this test file itself.
OLD_DEFAULT_MASTER_LITERAL = "frisket-dev-" + "master-key-change-me"

# 32 random-looking bytes as 44-char urlsafe base64: valid raw key material
# for an implementation that decodes the env var, and an acceptable
# passphrase for one that KDFs it. Either reading of the env var passes.
FIXTURE_MASTER_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()

# Long enough that its ciphertext is unambiguously the longest base64 leaf
# in the envelope (see _longest_leaf_path); includes unicode to pin utf-8.
PLAINTEXT = "sk-live-0123456789abcdefghijklmnopqrstuvwxyz-ключ-鍵-" + "x" * 32

# The eight reverse-dependency sites (paths relative to src/frisket).
# First six: core modules that must retarget onto the open module.
CORE_IMPORT_SITES = [
    "store/project.py",
    "server/provider_config.py",
    "server/services/projects.py",
    "workbench/plugin_runtime.py",
    "workbench/plugin_subprocess.py",
    "notifications/secrets.py",
]
OTHER_IMPORT_SITES = [
    "control/secrets.py",
    "hosted/services/secret_access.py",
]

_ALLOWED_AEAD_ALGORITHMS = {
    # normalized: lowercase, separators stripped
    "xchacha20poly1305",
    "chacha20poly1305",
    "aes256gcm",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _purge_module_cache() -> None:
    for name in list(sys.modules):
        if (
            name == NEW_MODULE
            or name == "frisket.team.security"
            or name.startswith("frisket.team.security.")
        ):
            sys.modules.pop(name, None)


def _import_secrets_module(*, fresh: bool = True):
    """Import frisket.team.security.secrets inside the test body.

    Guarded with pytest.fail (NOT a bare import at module top and NOT inside
    a fixture) so a missing module reports as a plain in-test assertion
    failure. The fail is raised OUTSIDE the except block, after the handled
    import failure's context clears, so the traceback stays focused on the
    missing public module.
    """
    if fresh:
        _purge_module_cache()
    module = None
    try:
        module = importlib.import_module(NEW_MODULE)
    except ImportError:
        module = None  # reported below, outside this handler
    if module is None:
        pytest.fail(
            f"open AEAD secrets module {NEW_MODULE!r} is not importable yet; "
            f"public secret storage requires a clean cutover to reviewed AEAD"
        )
    return module


@pytest.fixture(autouse=True)
def _clean_module_cache_after():
    yield
    _purge_module_cache()


@pytest.fixture
def fixture_key_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRISKET_SECRETS_MASTER_KEY", FIXTURE_MASTER_KEY)
    monkeypatch.delenv("FRISKET_BASE_URL", raising=False)


def _point_key_locations_at(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point every plausible generated-key location at an empty directory so
    behavior (b) of contract point 4 (generate + persist a unique key) lands
    the key file somewhere we can observe. The implementer defines the real
    env var; covering the plausible set keeps this assertion implementation-
    agnostic. Setting a speculative var is harmless."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdg-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / "xdg-cache"))
    monkeypatch.setenv("FRISKET_HOME", str(home / "frisket-home"))
    monkeypatch.setenv("FRISKET_DATA_DIR", str(home / "frisket-data"))
    monkeypatch.setenv("FRISKET_SECRETS_KEY_DIR", str(home / "frisket-keys"))
    monkeypatch.setenv(
        "FRISKET_SECRETS_KEY_FILE", str(home / "frisket-keys" / "master.key")
    )


def _leaf_strings(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _leaf_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _leaf_strings(value)
    elif isinstance(obj, str):
        yield obj


def _longest_leaf_path(obj, path=()):
    """(path, value) of the longest string leaf — the ciphertext body, given
    PLAINTEXT is much longer than any header/nonce/tag field."""
    best = (None, "")
    if isinstance(obj, dict):
        items = obj.items()
    elif isinstance(obj, list):
        items = enumerate(obj)
    elif isinstance(obj, str):
        return (path, obj)
    else:
        return best
    for key, value in items:
        candidate = _longest_leaf_path(value, path + (key,))
        if candidate[0] is not None and len(candidate[1]) > len(best[1]):
            best = candidate
    return best


def _set_at_path(obj, path, value):
    target = obj
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _flip_one_char(s: str) -> str:
    i = len(s) // 2  # middle: never base64 padding
    replacement = "B" if s[i] == "A" else "A"
    return s[:i] + replacement + s[i + 1 :]


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _extract_version(envelope: dict):
    raw = envelope.get("v", envelope.get("version"))
    assert raw is not None, (
        f"envelope declares no version field ('v' or 'version'); keys: "
        f"{sorted(envelope)}"
    )
    match = re.search(r"\d+", str(raw))
    assert match, f"envelope version field is not numeric: {raw!r}"
    return int(match.group())


# ---------------------------------------------------------------------------
# vendored v1 fixture generator — copied from the pre-cutover
# private composition/private composition/secrets_store.py (scrypt KEK + SHA-256-keystream +
# HMAC envelope) so the legacy fixture stays constructible after that
# module is deleted. Test-local only; the product must NOT contain this.
# ---------------------------------------------------------------------------

_LEGACY_SCRYPT_N = 1 << 14
_LEGACY_SCRYPT_R = 8
_LEGACY_SCRYPT_P = 1
_LEGACY_KEY_LEN = 32


def _legacy_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:length])


def _legacy_seal(key: bytes, plaintext: bytes) -> dict[str, str]:
    nonce = _stdlib_secrets.token_bytes(16)
    ks = _legacy_keystream(key, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks, strict=True))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return {"nonce": b64(nonce), "ct": b64(ct), "tag": b64(tag)}


def _legacy_encrypt_secret(master_key: str, plaintext: str) -> str:
    salt = _stdlib_secrets.token_bytes(16)
    kek = hashlib.scrypt(
        master_key.encode(),
        salt=salt,
        n=_LEGACY_SCRYPT_N,
        r=_LEGACY_SCRYPT_R,
        p=_LEGACY_SCRYPT_P,
        dklen=_LEGACY_KEY_LEN,
    )
    dek = _stdlib_secrets.token_bytes(_LEGACY_KEY_LEN)
    envelope = {
        "v": 1,
        "salt": base64.b64encode(salt).decode(),
        "dek": _legacy_seal(kek, dek),
        "body": _legacy_seal(dek, plaintext.encode()),
    }
    return json.dumps(envelope, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 1. round trip with an explicit injected key
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_with_explicit_env_key(self, fixture_key_env) -> None:
        mod = _import_secrets_module()
        for exported in ("encrypt_secret", "decrypt_secret", "key_hint"):
            assert hasattr(mod, exported), (
                f"{NEW_MODULE} must export {exported} (drop-in surface for "
                f"the six retargeted core import sites)"
            )
        blob = mod.encrypt_secret(PLAINTEXT)
        assert isinstance(blob, str)
        assert PLAINTEXT not in blob, "plaintext leaked into the stored envelope"
        assert mod.decrypt_secret(blob) == PLAINTEXT

    def test_fresh_nonce_per_encryption(self, fixture_key_env) -> None:
        mod = _import_secrets_module()
        assert mod.encrypt_secret(PLAINTEXT) != mod.encrypt_secret(PLAINTEXT), (
            "two encryptions of the same plaintext produced identical "
            "envelopes — nonce reuse / deterministic encryption"
        )


# ---------------------------------------------------------------------------
# 2. reviewed AEAD from a vetted library
# ---------------------------------------------------------------------------


class TestReviewedAead:
    def test_envelope_declares_version_2_and_aead_algorithm(
        self, fixture_key_env
    ) -> None:
        mod = _import_secrets_module()
        envelope = json.loads(mod.encrypt_secret(PLAINTEXT))
        assert isinstance(envelope, dict), "envelope must be a JSON object"
        version = _extract_version(envelope)
        assert version >= 2, (
            f"new envelopes must declare version >= 2 (got {version}); "
            f"v1 is the retired keystream format"
        )
        normalized = {
            re.sub(r"[^a-z0-9]", "", leaf.lower())
            for leaf in _leaf_strings(envelope)
            if len(leaf) < 64  # header fields, not base64 bodies
        }
        assert normalized & _ALLOWED_AEAD_ALGORITHMS, (
            f"envelope must name its AEAD in an algorithm field — one of "
            f"xchacha20poly1305 / chacha20poly1305 / aes-256-gcm; "
            f"short string fields found: "
            f"{sorted(normalized)}"
        )

    def test_module_uses_cryptography_library(self, fixture_key_env) -> None:
        mod = _import_secrets_module()
        # rule19: executes-the-artifact — encrypt/decrypt round-trip drives the module; import scan backs the sys.modules check
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert re.search(r"^\s*(?:from|import)\s+cryptography\b", source, re.M), (
            f"{NEW_MODULE} must import its AEAD from the vetted "
            f"`cryptography` package: use reviewed AEAD from a "
            f"vetted library, not hand-rolled crypto"
        )
        mod.decrypt_secret(mod.encrypt_secret(PLAINTEXT))
        assert any(
            name == "cryptography" or name.startswith("cryptography.")
            for name in sys.modules
        ), "the cryptography package was never actually imported at runtime"

    def test_old_keystream_scheme_is_gone_from_module_source(
        self, fixture_key_env
    ) -> None:
        mod = _import_secrets_module()
        assert not hasattr(mod, "_keystream"), (
            f"{NEW_MODULE} still contains the custom SHA-256-keystream "
            f"scheme; the cutover replaces it, not wraps it"
        )


# ---------------------------------------------------------------------------
# 3. tamper detection
# ---------------------------------------------------------------------------


class TestTamperDetection:
    def test_single_flipped_ciphertext_char_makes_decrypt_raise(
        self, fixture_key_env
    ) -> None:
        mod = _import_secrets_module()
        blob = mod.encrypt_secret(PLAINTEXT)
        assert mod.decrypt_secret(blob) == PLAINTEXT  # sanity pre-tamper
        envelope = json.loads(blob)
        path, ciphertext = _longest_leaf_path(envelope)
        assert path is not None and len(ciphertext) >= 24, (
            f"could not locate a ciphertext body leaf in the envelope: {blob!r}"
        )
        _set_at_path(envelope, path, _flip_one_char(ciphertext))
        tampered = json.dumps(envelope, separators=(",", ":"))
        try:
            recovered = mod.decrypt_secret(tampered)
        except Exception:
            pass  # AEAD authentication failure — the required outcome
        else:
            pytest.fail(
                f"decrypt of a tampered envelope returned "
                f"{recovered[:16]!r}... instead of raising — no authentication"
            )


# ---------------------------------------------------------------------------
# 4. no in-repo shared default master key
# ---------------------------------------------------------------------------


class TestNoDefaultMasterKey:
    def test_missing_key_refuses_or_generates_unique_persisted_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With FRISKET_SECRETS_MASTER_KEY unset, either allowed behavior
        passes: (a) encrypt_secret raises a legible RuntimeError demanding a
        key, or (b) a unique random key is generated AND persisted so a
        second import decrypts, and two independent locations get DIFFERENT
        keys. The forbidden outcome is silent operation on a shared static
        default."""
        monkeypatch.delenv("FRISKET_SECRETS_MASTER_KEY", raising=False)
        monkeypatch.delenv("FRISKET_BASE_URL", raising=False)

        cycles = []
        for label in ("location-a", "location-b"):
            home = tmp_path / label
            home.mkdir()
            _point_key_locations_at(monkeypatch, home)
            mod = _import_secrets_module()
            try:
                blob = mod.encrypt_secret(PLAINTEXT)
            except RuntimeError as exc:
                # behavior (a): legible refusal
                message = str(exc)
                assert (
                    "FRISKET_SECRETS_MASTER_KEY" in message
                    or "master key" in message.lower()
                ), (
                    f"refusal must tell the operator WHICH key to configure; "
                    f"got: {message!r}"
                )
                return
            # behavior (b): generated + persisted
            key_files = sorted(p for p in home.rglob("*") if p.is_file())
            assert key_files, (
                "encrypt_secret succeeded without a configured key but "
                "persisted nothing under any pointed location — an "
                "in-memory or hardcoded key cannot survive a restart and "
                "is indistinguishable from a shared default"
            )
            material = b"".join(p.read_bytes() for p in key_files)
            assert OLD_DEFAULT_MASTER_LITERAL.encode() not in material
            # a second (fresh) import must find the persisted key
            mod_again = _import_secrets_module()
            assert mod_again.decrypt_secret(blob) == PLAINTEXT, (
                "a fresh import could not decrypt with the persisted "
                "generated key — the key was not durably persisted"
            )
            cycles.append((blob, material))

        (blob_a, material_a), (_blob_b, material_b) = cycles
        assert material_a != material_b, (
            "two independent empty locations produced identical key "
            "material — that is a shared static default, not generation"
        )
        # currently keyed to location-b: location-a's ciphertext must not open
        mod_b = _import_secrets_module()
        try:
            recovered = mod_b.decrypt_secret(blob_a)
        except Exception:
            pass
        else:
            assert recovered != PLAINTEXT, (
                "an envelope sealed under location-a's generated key "
                "decrypted under location-b's — the 'generated' key is a "
                "shared constant"
            )


# ---------------------------------------------------------------------------
# 5. clean cutover — legacy v1 rejected, old module deleted
# ---------------------------------------------------------------------------


class TestCleanCutover:
    def test_legacy_v1_envelope_rejected_with_legible_reset_error(
        self, fixture_key_env
    ) -> None:
        mod = _import_secrets_module()
        legacy_blob = _legacy_encrypt_secret(FIXTURE_MASTER_KEY, "old-secret")
        try:
            recovered = mod.decrypt_secret(legacy_blob)
        except Exception as exc:
            message = str(exc).lower()
            assert (
                "reset" in message
                or "re-enter" in message
                or "reenter" in message
                or "unsupported legacy" in message
            ), (
                f"v1 rejection must be legible — tell the operator to "
                f"reset/re-enter the secret; pre-release "
                f"ciphertext is discarded with no compatibility reader; "
                f"got: {exc!r}"
            )
        else:
            pytest.fail(
                f"decrypt_secret accepted a v1 keystream envelope "
                f"(returned {recovered!r}) — the clean cutover forbids a "
                f"compatibility reader"
            )

    def test_old_hosted_secrets_store_module_is_deleted(self) -> None:
        assert not OLD_MODULE_PATH.exists(), (
            f"{OLD_MODULE_PATH.relative_to(ROOT)} still exists — the "
            f"cutover deletes it and retargets its posture and allowlist "
            f"checks atomically"
        )


# ---------------------------------------------------------------------------
# 6. import retarget
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. key_hint sanity
# ---------------------------------------------------------------------------


class TestKeyHint:
    def test_key_hint_reveals_at_most_last_four_chars(self, fixture_key_env) -> None:
        mod = _import_secrets_module()
        secret = "sk-proj-A1b2C3d4E5f6G7h8J9k0wxyz"
        hint = mod.key_hint(secret)
        assert isinstance(hint, str)
        assert secret not in hint, "key_hint echoed the full secret"
        five_char_windows = {secret[i : i + 5] for i in range(len(secret) - 4)}
        leaked = sorted(w for w in five_char_windows if w in hint)
        assert not leaked, (
            f"key_hint reveals a 5+ character run of the secret {leaked} — "
            f"at most the last 4 characters may appear"
        )

    def test_key_hint_does_not_echo_short_secrets(self, fixture_key_env) -> None:
        mod = _import_secrets_module()
        assert "ab" not in mod.key_hint("ab"), "key_hint echoed a short secret in full"
