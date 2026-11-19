"""A notification emitter that swallows a failure logs which emitter failed.

The export, feed-health and delivery-health emitters keep their action, poll
or delivery outcome authoritative by never raising; the warning is what makes
a lost notification visible. It names the emitter and the exception type and
never carries the exception's text.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from frisket.engine.executor.action_families import exports
from frisket.engine.jobs import feeds, notifications_delivery

SECRET_DETAIL = "payload detail that must not be logged"


def _refuse(*_args, **_kwargs):
    raise RuntimeError(SECRET_DETAIL)


def _emit_export(monkeypatch):
    monkeypatch.setattr(exports, "export_notification_candidate", lambda **_: object())
    monkeypatch.setattr(exports, "emit_notification_candidate", _refuse)
    result = SimpleNamespace(
        status="completed",
        outputs=[],
        errors=[],
        project_id="p",
        receipt_id="r",
        action=SimpleNamespace(kind="export.csv"),
    )
    exports._emit_export_notification(object(), result)


def _emit_feed_health(monkeypatch):
    monkeypatch.setattr(
        feeds, "feed_health_notification_candidate", lambda **_: object()
    )
    monkeypatch.setattr(feeds, "emit_notification_candidate", _refuse)
    feeds._emit_feed_health_notification(
        object(), project_id="p", feed={"id": 1, "name": "Feed"}, status="error"
    )


def _emit_delivery_health(monkeypatch):
    monkeypatch.setattr(
        notifications_delivery,
        "delivery_health_notification_candidate",
        lambda **_: object(),
    )
    monkeypatch.setattr(notifications_delivery, "emit_notification_candidate", _refuse)
    request = {
        "id": 1,
        "route_id": None,
        "channel_id": None,
        "notification_id": None,
        "job_id": None,
    }
    notifications_delivery._emit_delivery_health_notification(
        object(), project_id="p", request=request, status="failed", error=None
    )


EMITTERS = {
    "_emit_export_notification": _emit_export,
    "_emit_feed_health_notification": _emit_feed_health,
    "_emit_delivery_health_notification": _emit_delivery_health,
}


@pytest.mark.parametrize("emitter", sorted(EMITTERS))
def test_swallowed_emitter_failure_is_logged_without_its_detail(
    emitter, monkeypatch, caplog
):
    with caplog.at_level(logging.WARNING):
        EMITTERS[emitter](monkeypatch)

    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "notification_emit_failed"
    ]
    assert [(r.emitter, r.error_type) for r in failures] == [(emitter, "RuntimeError")]
    assert SECRET_DETAIL not in caplog.text
