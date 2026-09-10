"""Exercise the packaged media tools through Frisket's production extractor."""

from __future__ import annotations

import binascii
import hashlib
import json
import struct
import subprocess
import tempfile
import wave
import zlib
from pathlib import Path

from pypdf import PdfWriter

from frisket.ops.media_metadata import (
    _resolve_exiftool_path,
    _resolve_ffprobe_path,
    extract_media_metadata,
)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    )


def _write_png(path: Path) -> None:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )


def main() -> None:
    notice = Path("/app/THIRD_PARTY_NOTICES.md")
    assert notice.is_file()
    notice_text = notice.read_text(encoding="utf-8")
    assert "ExifTool" in notice_text
    assert "FFmpeg / ffprobe" in notice_text
    assert Path("/usr/local/share/doc/exiftool/README").is_file()
    assert Path("/usr/share/doc/perl/copyright").is_file()

    with tempfile.TemporaryDirectory(prefix="frisket-metadata-smoke-") as raw:
        root = Path(raw)
        image = root / "image.png"
        audio = root / "audio.wav"
        video = root / "video.mp4"
        document = root / "document.pdf"
        _write_png(image)
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            output.writeframes(b"\x00\x00" * 800)
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:d=0.1",
                "-pix_fmt",
                "yuv420p",
                "-y",
                str(video),
            ],
            check=True,
        )
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_metadata({"/Subject": "00123"})
        with document.open("wb") as output:
            writer.write(output)

        probe = subprocess.run(
            [
                "exiftool",
                "-json",
                "-FileType",
                "-MIMEType",
                "-ImageWidth",
                "-ImageHeight",
                "-Duration",
                "-PageCount",
                "--",
                *(str(path) for path in (image, audio, video, document)),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = json.loads(probe.stdout)
        assert len(rows) == 4
        assert {row.get("FileType") for row in rows} == {"PNG", "WAV", "MP4", "PDF"}

        # Also cross the action's path resolution and bounded subprocess adapter.
        exiftool = _resolve_exiftool_path()
        ffprobe = _resolve_ffprobe_path()
        assert exiftool == Path("/usr/local/bin/exiftool").resolve()
        assert ffprobe == Path("/usr/bin/ffprobe").resolve()
        protocols = subprocess.run(
            [str(ffprobe), "-v", "error", "-protocols"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        input_protocols = protocols.partition("Output:")[0].splitlines()
        assert any(line.strip() == "fd" for line in input_protocols)

        video_bytes = video.read_bytes()
        mdat_offset = video_bytes.find(b"mdat")
        moov_offset = video_bytes.find(b"moov")
        assert 0 <= mdat_offset < moov_offset

        fixtures = (
            (image, "image", "png", "image/png"),
            (audio, "audio", "wav", "audio/wav"),
            (video, "video", "mp4", "video/mp4"),
            (document, "pdf", "pdf", "application/pdf"),
        )
        for path, expected_kind, expected_format, claimed_mime in fixtures:
            payload = path.read_bytes()
            envelope = extract_media_metadata(
                path,
                digest=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                filename=path.name,
                claimed_mime=claimed_mime,
            )
            serialized = json.dumps(envelope, ensure_ascii=False, sort_keys=True)
            normalized = envelope["normalized"]
            adapters = envelope["extraction"]["adapters"]
            assert str(root.resolve()) not in serialized
            assert "frisket-metadata-adapter-" not in serialized
            assert "adapter_transport" in envelope["omitted"]["classes"]
            assert normalized["kind"] == expected_kind
            assert normalized["format"] == expected_format
            assert normalized["probe_status"] == "ok"
            assert adapters["exiftool"]["outcome"] == "success"
            assert adapters["exiftool"]["version"] == "13.59"
            if expected_kind in {"audio", "video"}:
                assert adapters["ffprobe"]["outcome"] == "success"
                assert adapters["ffprobe"]["version"]
            if expected_kind == "video":
                video_stream = next(
                    stream
                    for stream in envelope["structure"]["streams"]
                    if stream["type"] == "video"
                )
                assert video_stream["pixel_format"]
            if expected_kind == "pdf":
                pdf_info = envelope["sources"]["pdf_info"]
                assert type(pdf_info["pdf.page_count"]) is int
                assert pdf_info["pdf.subject"] == "00123"


if __name__ == "__main__":
    main()
