"""The managed bootstrap accepts data, then installs policy before workers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from frisket.engine.sandbox import _bootstrap_policy


POLICY = {
    "version": 1,
    "audit_netwall": True,
    "fence": {
        "allow_unix_sockets": False,
        "read": ["/tmp/input"],
        "write": ["/tmp/output"],
        "allow_exec": False,
        "python_roots": True,
    },
}


def test_policy_is_strict_data_with_absolute_paths():
    assert _bootstrap_policy._validated(POLICY) == (
        True,
        (False, ("/tmp/input",), ("/tmp/output",), False, True),
    )
    with pytest.raises(ValueError, match="extra=.*source"):
        _bootstrap_policy._validated({**POLICY, "source": "print('no')"})
    with pytest.raises(ValueError, match="absolute"):
        _bootstrap_policy._validated(
            {**POLICY, "fence": {**POLICY["fence"], "read": ["relative"]}}
        )


def test_kernel_fence_precedes_audit_hook(monkeypatch):
    calls: list[tuple[object, ...]] = []
    helpers = {
        "_child_fence": SimpleNamespace(
            _frisket_fence_install=lambda *args: calls.append(("fence", *args))
        ),
        "_netwall": SimpleNamespace(install=lambda: calls.append(("audit",))),
    }
    monkeypatch.setattr(_bootstrap_policy, "_load_sibling", helpers.__getitem__)

    _bootstrap_policy.install(POLICY)

    assert calls == [
        ("fence", False, ("/tmp/input",), ("/tmp/output",), False, True),
        ("audit",),
    ]


def test_installer_failure_emits_refusal_marker_and_exits(monkeypatch, capsys):
    class Refused(Exception):
        pass

    def broken(*_args):
        raise RuntimeError("kernel refused")

    monkeypatch.setattr(
        _bootstrap_policy,
        "_load_sibling",
        lambda name: SimpleNamespace(
            _frisket_fence_install=broken, install=lambda: None
        ),
    )
    monkeypatch.setattr(
        _bootstrap_policy.os,
        "_exit",
        lambda code: (_ for _ in ()).throw(Refused(code)),
    )

    with pytest.raises(Refused, match="126"):
        _bootstrap_policy.install(POLICY)

    assert capsys.readouterr().err.startswith(
        "FRISKET_SANDBOX_FENCE_UNAVAILABLE: RuntimeError: kernel refused"
    )


def test_policy_loader_names_exact_sibling_file():
    child = _bootstrap_policy._load_sibling("_child_fence")
    assert child.__file__ == str(
        _bootstrap_policy.Path(_bootstrap_policy.__file__).with_name("_child_fence.py")
    )
