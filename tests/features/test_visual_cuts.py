from __future__ import annotations

import scenedetect

from frisket.features.temporal.visual_cuts import detect_visual_cuts, visual_cuts_value


class _Timecode:
    def __init__(self, seconds: float, frame: int, text: str) -> None:
        self._seconds = seconds
        self._frame = frame
        self._text = text

    def get_seconds(self) -> float:
        return self._seconds

    def get_frames(self) -> int:
        return self._frame

    def get_timecode(self) -> str:
        return self._text


def _install_fake_detector(monkeypatch) -> None:
    scenes = [
        (_Timecode(0, 0, "00:00:00.000"), _Timecode(1, 30, "00:00:01.000")),
        (_Timecode(1.2345, 37, "00:00:01.235"), _Timecode(3, 90, "00:00:03.000")),
        (_Timecode(4.0, 120, "00:00:04.000"), _Timecode(5, 150, "00:00:05.000")),
    ]

    class FakeManager:
        def add_detector(self, detector) -> None:
            self.detector = detector

        def detect_scenes(self, *, video, show_progress: bool) -> None:
            assert video == "video"
            assert show_progress is False

        def get_scene_list(self, *, start_in_scene: bool):
            assert start_in_scene is True
            return scenes

    class FakeDetector:
        def __init__(self, *, threshold: float, min_scene_len: int) -> None:
            assert threshold == 27.0
            assert min_scene_len == 15

    monkeypatch.setattr(scenedetect, "open_video", lambda _path: "video")
    monkeypatch.setattr(scenedetect, "SceneManager", FakeManager)
    monkeypatch.setattr(scenedetect, "ContentDetector", FakeDetector)


def test_detect_visual_cuts_keeps_native_frame_facts(monkeypatch) -> None:
    _install_fake_detector(monkeypatch)

    cuts = detect_visual_cuts("sample.mp4")

    assert [(cut.at_ms, cut.frame, cut.timecode) for cut in cuts] == [
        (1235, 37, "00:00:01.235"),
        (4000, 120, "00:00:04.000"),
    ]


def test_visual_cuts_value_binds_and_drops_endpoint(monkeypatch) -> None:
    _install_fake_detector(monkeypatch)

    value = visual_cuts_value(
        "sample.mp4",
        timeline={
            "artifact_stable_id": "source_artifact:video-1",
            "fingerprint": "sha256:" + "a" * 64,
            "duration_ms": 4000,
        },
    )

    assert value["schema_version"] == "frisket.timeline_points.v1"
    assert [(item["id"], item["at_ms"]) for item in value["items"]] == [
        ("visual-cut-0000000037", 1235)
    ]
    assert value["items"][0]["metadata"]["detector"] == "pyscenedetect.content"
