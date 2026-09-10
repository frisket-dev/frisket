"""Rights-cleared local audio excerpts for the council listening walkthrough."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class SampleCouncilAudio:
    filename: str
    meeting: str
    meeting_date: str
    source_url: str
    source_sha1: str
    clip_sha256: str
    license: str = "CC0-1.0"
    author: str = "Michelle Ryan Hughes"
    clip_start_seconds: int = 0
    clip_end_seconds: int = 300


SAMPLE_COUNCIL_AUDIO: tuple[SampleCouncilAudio, ...] = (
    SampleCouncilAudio(
        filename="ann-arbor-policy-committee-2022-04-19-excerpt.mp3",
        meeting="Ann Arbor City Council Policy Committee",
        meeting_date="2022-04-19",
        source_url=(
            "https://commons.wikimedia.org/wiki/File:PolicyCommittee-2022-04-19.ogg"
        ),
        source_sha1="e64e736d7b2ca9fe86a8d8bf8f9b4ed2a7106cb5",
        clip_sha256="caf92a27a49ef8980568e5581250144350b8e6b632ceb4c3d6d9c4b8b3aa3ad9",
    ),
    SampleCouncilAudio(
        filename="ann-arbor-icpoc-2020-07-01-excerpt.mp3",
        meeting="Ann Arbor Independent Community Police Oversight Commission",
        meeting_date="2020-07-01",
        source_url=("https://commons.wikimedia.org/wiki/File:ICPOC-2020-07-01.ogg"),
        source_sha1="e2b4de4d8399033cb41359fcef042e41a682b056",
        clip_sha256="ca0170c217a23c9c5989d359b39b06b58d720b52d9e0eb2ef85936f9e9ec3f61",
    ),
)


def council_audio_bytes(filename: str) -> bytes:
    return resources.files(__package__).joinpath(filename).read_bytes()
