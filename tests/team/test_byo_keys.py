from __future__ import annotations

import base64
import json
import subprocess
import sys


from frisket.team.security.secrets import decrypt_secret, encrypt_secret, key_hint


def test_envelope_roundtrip_and_never_stores_plaintext():
    secret = "sk-ant-supersecret-value"
    blob = encrypt_secret(secret)
    assert secret not in blob  # ciphertext only
    assert decrypt_secret(blob) == secret


def test_envelope_detects_tampering():
    blob = encrypt_secret("sk-ant-abcd")
    env = json.loads(blob)
    ct = bytearray(base64.b64decode(env["ct"]))
    ct[0] ^= 0xFF
    env["ct"] = base64.b64encode(bytes(ct)).decode()
    try:
        decrypt_secret(json.dumps(env))
        raise AssertionError("tampered ciphertext should not decrypt")
    except ValueError:
        pass


def test_key_hint_reveals_only_tail():
    assert key_hint("sk-ant-abcd1234") == "...1234"
    assert "sk-ant" not in key_hint("sk-ant-abcd1234")


def test_two_processes_load_or_create_secret_key_converge_on_same_bytes(tmp_path):
    """Two independent processes call load_or_create_secret_key on the same
    path at once. Both must return the SAME 32 bytes (one process creates
    the key, the other reads it back -- never two different generated
    keys), and the persisted file must be exactly those bytes. This proves
    the generated-secret-key filelock.FileLock, not
    merely a same-process race."""
    key_path = tmp_path / "master.key"
    code = (
        "import sys\n"
        "from frisket.team.security.secrets import load_or_create_secret_key\n"
        "key = load_or_create_secret_key(sys.argv[1])\n"
        "sys.stdout.buffer.write(key)\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(key_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = [p.communicate(timeout=30) for p in processes]
    assert [p.returncode for p in processes] == [0, 0], results
    keys = [stdout for stdout, _stderr in results]
    assert len(keys[0]) == 32 and len(keys[1]) == 32
    assert keys[0] == keys[1]
    assert key_path.read_bytes() == keys[0]
