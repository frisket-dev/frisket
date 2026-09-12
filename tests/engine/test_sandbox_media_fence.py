"""What the document and media pipeline is actually confined by.

subprocess-boundary: the claim under test IS the sandbox isolation of a real
child -- seccomp filters and Landlock rulesets are per-process kernel state
installed between fork and exec, so an in-process invoker cannot exercise any
of it. Every spawn here asserts a genuine process property.

`tests/engine/test_sandbox_recipe_fence.py` pins the same two kernel
mechanisms for `map.python`, where the operator chose to run code. This file
pins them for the ops where nobody chose anything: a journalist imported a PDF
out of a FOIA dump or a video from a source, and poppler, ffmpeg, OpenCV and
ONNX Runtime parse it. Those are the runtimes with the CVE histories, and
their legitimate needs are exactly what Landlock expresses -- read one file,
write one directory -- so confining them costs no feature.

Executed against the real op methods, on Linux, these hold:

  HOLDS  each op still produces the same bytes confined as unconfined, on a
         real fixture, through the real converter
  HOLDS  a confined op cannot read a file it did not declare, even though the
         same op unconfined reads it happily
  HOLDS  the native lane (ffmpeg/ffprobe/pdftoppm) is confined too, via a
         launcher that installs the fence and `execv`s into the binary,
         keeping the pid the process supervisor owns
  HOLDS  the declared profile is load-bearing rather than decorative:
         dropping poppler's /etc/fonts grant changes the rendered pixels
  HOLDS  a fence installer failure is a refusal, not a quiet unconfined run;
         the named bootstrap policy boundary pins that classification
  HOLDS  a kernel that cannot fence at all DEGRADES LOUDLY instead of
         refusing. That is the one place this lane's posture differs from the
         recipe lane's, and the reason is in `fence.warn_confinement_unavailable`

Linux-only by construction. A skip on another platform is honest; a failure
is real.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="pins the Linux seccomp/Landlock fence; other platforms have none",
)

FIXTURE_PDF = (
    Path(__file__).resolve().parents[1] / "fixtures" / "ntsb" / "ERA23LA107.pdf"
)


@pytest.fixture(autouse=True)
def _kernel_can_fence():
    """These are proofs about a fence, so a kernel without one must skip."""
    reason = fence.kernel_can_confine()
    if reason is not None:
        pytest.skip(f"this kernel cannot install the fence: {reason}")


def _nonembedded_font_pdf() -> bytes:
    """A one-page PDF naming Helvetica without embedding it.

    Poppler has to resolve a substitute through fontconfig to render this,
    which is what makes the /etc/fonts grant observable.
    """
    content = b"BT /F1 24 Tf 72 700 Td (FRISKET FENCE 4271) Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources "
        b"<< /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += str(index).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _digests(directory: Path, pattern: str) -> list[str]:
    return [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob(pattern))
    ]


# --- the native lane: poppler and ffmpeg -----------------------------------


def _rasterize(
    pdf: Path, out_dir: Path, *, confined: bool, fonts: bool = True, dpi: int = 50
) -> None:
    pdftoppm = shutil.which("pdftoppm")
    read = (str(pdf), "/etc/fonts") if fonts else (str(pdf),)
    profile = fence.Confinement(
        op="ocr (pdftoppm rasterization)",
        read=read,
        write=(str(out_dir),),
        exec_binary=pdftoppm,
    )
    result = asyncio.run(
        run_sandboxed(
            [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(out_dir / "page")],
            policy=SandboxPolicy(
                wall_seconds=120,
                memory_mb=2048,
                env_passthrough=["PATH"],
                confine=profile if confined else None,
            ),
        )
    )
    assert result.returncode == 0, result.stderr[-800:]


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_the_real_ocr_rasterization_still_produces_the_same_pages(tmp_path):
    """The whole point: confinement must cost the user nothing.

    The confined side is `OcrEngines._page_images` itself, not a copy of its
    policy -- a test that rebuilt the profile would stay green while the op's
    own profile drifted away from it.
    """
    from frisket.ops.ocr_engines import OcrEngines

    confined, plain = tmp_path / "confined", tmp_path / "plain"
    confined.mkdir()
    plain.mkdir()
    _rasterize(FIXTURE_PDF, plain, confined=False, dpi=72)
    pages = asyncio.run(
        OcrEngines()._page_images(
            FIXTURE_PDF,
            {"mime": "application/pdf", "filename": FIXTURE_PDF.name},
            {"dpi": 72},
            confined,
        )
    )
    assert _digests(plain, "page-*.png"), "the unconfined render produced no pages"
    assert [page.name for page in pages] == sorted(
        path.name for path in plain.glob("page-*.png")
    )
    assert _digests(confined, "page-*.png") == _digests(plain, "page-*.png")


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_poppler_needs_its_font_config_grant_and_fails_silently_without_it(tmp_path):
    """Why /etc/fonts is in the profile, kept as a test rather than a comment.

    A PDF whose fonts are not embedded still rasterizes without the grant and
    still exits 0 -- fontconfig just falls back -- so the failure would reach
    the user as OCR reading the wrong glyphs, with nothing in the logs to
    connect it to the fence. That silence is the reason this is pinned.
    """
    pdf = tmp_path / "nonembedded.pdf"
    pdf.write_bytes(_nonembedded_font_pdf())
    with_fonts, without_fonts, plain = (
        tmp_path / "with",
        tmp_path / "without",
        tmp_path / "plain",
    )
    for directory in (with_fonts, without_fonts, plain):
        directory.mkdir()
    _rasterize(pdf, plain, confined=False)
    _rasterize(pdf, with_fonts, confined=True, fonts=True)
    _rasterize(pdf, without_fonts, confined=True, fonts=False)
    assert _digests(with_fonts, "page-*.png") == _digests(plain, "page-*.png")
    assert _digests(without_fonts, "page-*.png") != _digests(plain, "page-*.png"), (
        "dropping the /etc/fonts grant changed nothing, so either poppler no "
        "longer resolves substitute fonts through fontconfig or this PDF now "
        "embeds its font -- the grant's justification needs re-deriving"
    )


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler not installed")
def test_a_confined_pdftoppm_cannot_open_a_pdf_it_was_not_given(tmp_path):
    """The boundary is the declared input, not "any PDF this user can read".

    The same command, the same binary, the same policy but for the
    confinement: unconfined it reads the other document, confined it cannot.
    """
    other = tmp_path / "not-declared.pdf"
    other.write_bytes(FIXTURE_PDF.read_bytes())
    declared = tmp_path / "declared.pdf"
    declared.write_bytes(_nonembedded_font_pdf())
    out = tmp_path / "out"
    out.mkdir()
    pdftoppm = shutil.which("pdftoppm")
    argv = [pdftoppm, "-png", "-r", "50", str(other), str(out / "page")]

    unconfined = asyncio.run(
        run_sandboxed(
            argv, policy=SandboxPolicy(wall_seconds=60, env_passthrough=["PATH"])
        )
    )
    assert unconfined.returncode == 0 and _digests(out, "page-*.png")

    for path in out.glob("page-*.png"):
        path.unlink()
    confined = asyncio.run(
        run_sandboxed(
            argv,
            policy=SandboxPolicy(
                wall_seconds=60,
                env_passthrough=["PATH"],
                confine=fence.Confinement(
                    op="ocr (pdftoppm rasterization)",
                    read=(str(declared), "/etc/fonts"),
                    write=(str(out),),
                    exec_binary=pdftoppm,
                ),
            ),
        )
    )
    assert confined.returncode != 0, "the confined child read an undeclared PDF"
    assert not _digests(out, "page-*.png"), "the confined child wrote pages anyway"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)
def test_a_confined_ffmpeg_extracts_the_same_frame(tmp_path):
    """The native lane end to end: probe the container, then cut a frame."""
    video = tmp_path / "sample.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        timeout=120,
    )
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")

    probe = asyncio.run(
        run_sandboxed(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(video),
            ],
            policy=SandboxPolicy(
                wall_seconds=60,
                env_passthrough=["PATH"],
                confine=fence.Confinement(
                    op="media.video_frames (ffprobe)",
                    read=(str(video),),
                    exec_binary=ffprobe,
                ),
            ),
        )
    )
    assert probe.returncode == 0, probe.stderr[-500:]
    assert float(json.loads(probe.stdout)["format"]["duration"]) == pytest.approx(
        2.0, abs=0.2
    )

    frames = {}
    for label, confined in (("plain", False), ("confined", True)):
        out = tmp_path / f"{label}.jpg"
        result = asyncio.run(
            run_sandboxed(
                [
                    ffmpeg,
                    "-loglevel",
                    "error",
                    "-ss",
                    "1.00",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "4",
                    "-y",
                    str(out),
                ],
                policy=SandboxPolicy(
                    wall_seconds=120,
                    memory_mb=2048,
                    env_passthrough=["PATH"],
                    confine=(
                        fence.Confinement(
                            op="media.video_frames (ffmpeg)",
                            read=(str(video),),
                            write=(str(tmp_path),),
                            exec_binary=ffmpeg,
                        )
                        if confined
                        else None
                    ),
                ),
            )
        )
        assert result.returncode == 0, result.stderr[-500:]
        frames[label] = out.read_bytes()
    assert frames["plain"] and frames["confined"] == frames["plain"]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)
def test_the_real_video_frames_op_still_extracts_frames_confined(tmp_path):
    """Real ffprobe/ffmpeg through the admitted reader and production stager."""
    from contextlib import closing
    from frisket.engine.store import Project
    from frisket.actions.types import ColumnRef
    from frisket.actions.row_media_types import FrameCount
    from tests.engine.test_row_media_reader import bound_media

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=160x120:rate=10",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=120,
    )

    async def run(project):
        async with bound_media(project, payload=source.read_bytes()) as (
            _,
            reader,
            row,
            stager,
        ):
            frames = await reader.extract(
                row, ColumnRef("renamed"), sampling=FrameCount(count=2)
            )
            assert len(frames) == 2
            assert [frame.t for frame in frames] == [0.5, 1.5]
            for frame in frames:
                blob = stager._stager._admitted(frame.image)
                assert blob.mime == "image/jpeg"
                assert blob.path.read_bytes().startswith(bytes((255, 216)))

    with closing(Project.create(tmp_path / "frames.frisket")) as project:
        asyncio.run(run(project))


def test_the_real_extract_faces_op_reads_its_image_through_the_fence(
    tmp_path, monkeypatch
):
    """AdmittedFaceExtractor end to end, real OpenCV, confined.

    `execute` raises on the worker's `{"error": ...}`, and the worker reports
    "unreadable image" whenever `cv2.imread` returns None -- which is what a
    refused open looks like, since imread swallows the OSError. So a
    successful return IS the proof that the declared input was readable
    through the fence, and the detections are compared with an unconfined run
    of the same worker on the same bytes.
    """
    from contextlib import closing

    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    from frisket.engine.store import Project
    from frisket.actions.types import ColumnRef
    from frisket.engine.executor import row_media_read
    from frisket.runtime.launch import worker_argv
    from tests.engine.test_row_media_reader import bound_media

    async def checked_sandbox(*args, **kwargs):
        result = await run_sandboxed(*args, **kwargs)
        assert result.ok, result
        return result

    monkeypatch.setattr(row_media_read, "run_sandboxed", checked_sandbox)

    canvas = numpy.full((400, 400, 3), 235, dtype=numpy.uint8)
    cv2.ellipse(canvas, (200, 200), (95, 125), 0, 0, 360, (190, 190, 190), -1)
    cv2.ellipse(canvas, (165, 170), (14, 9), 0, 0, 360, (30, 30, 30), -1)
    cv2.ellipse(canvas, (235, 170), (14, 9), 0, 0, 360, (30, 30, 30), -1)
    source = tmp_path / "face.png"
    cv2.imwrite(str(source), canvas)

    async def run(project):
        async with bound_media(project, kind="image", payload=source.read_bytes()) as (
            _,
            reader,
            row,
            _,
        ):
            faces = await reader.extract(row, ColumnRef("renamed"))
            return [
                {key: getattr(face, key) for key in ("x", "y", "w", "h")}
                for face in faces
            ]

    with closing(Project.create(tmp_path / "faces.frisket")) as project:
        out = asyncio.run(run(project))

    unconfined = json.loads(
        asyncio.run(
            run_sandboxed(
                # subprocess-boundary: seccomp/Landlock are per-process kernel state installed between fork and exec; no in-process invoker can exercise them
                worker_argv("faces"),
                policy=SandboxPolicy(
                    wall_seconds=120,
                    memory_mb=2048,
                    env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                ),
                stdin_data=json.dumps(
                    {
                        "path": str(source),
                        "model_path": str(
                            Path(__file__).parents[2]
                            / "src/frisket/data/face_detection/face_detection_yunet_2023mar.onnx"
                        ),
                        "out_dir": str(tmp_path),
                    }
                ).encode(),
            )
        ).stdout
    )
    assert [{key: face[key] for key in ("x", "y", "w", "h")} for face in out] == [
        {key: face[key] for key in ("x", "y", "w", "h")} for face in unconfined["faces"]
    ]


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffmpeg not installed")
def test_a_confined_ffprobe_cannot_open_a_socket_of_its_own():
    """`allow_network=False` stops being a comment for the native lane.

    ffmpeg's demuxers can be told to fetch: `tcp://`, `http://`, an HLS
    playlist or a `concat` list inside a file a source sent. Unconfined,
    ffprobe really does connect -- this test's own listener accepts it -- and
    nothing below Python was stopping it, because the audit-hook netwall only
    ever governed CPython's `socket` module. Confined, `socket()` returns
    EPERM and no connection arrives.

    A loopback listener owned by the test is the target, so the proof is
    hermetic and needs no internet.
    """
    import socket
    import threading

    ffprobe = shutil.which("ffprobe")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    accepted: list[int] = []

    def accept_any() -> None:
        listener.settimeout(10)
        for _ in range(2):
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            accepted.append(1)
            connection.close()

    # A simulated clock cannot observe a kernel refusal; only a real listener
    # in a real thread proves no TCP connection arrives.
    # realtime: proving a kernel-level connect refusal needs a real listener
    accepter = threading.Thread(target=accept_any, daemon=True)
    accepter.start()

    def probe(confined: bool):
        return asyncio.run(
            run_sandboxed(
                [ffprobe, "-v", "error", "-i", f"tcp://127.0.0.1:{port}"],
                policy=SandboxPolicy(
                    wall_seconds=30,
                    memory_mb=2048,
                    env_passthrough=["PATH"],
                    confine=(
                        fence.Confinement(
                            op="media.video_frames (ffprobe)", exec_binary=ffprobe
                        )
                        if confined
                        else None
                    ),
                ),
            )
        )

    try:
        probe(confined=False)
        assert accepted, (
            "unconfined ffprobe did not reach the listener, so the confined "
            "comparison below would prove nothing"
        )
        reached_unconfined = len(accepted)
        confined = probe(confined=True)
    finally:
        listener.close()
        accepter.join(timeout=10)
    assert "not permitted" in confined.stderr, confined.stderr[:400]
    assert len(accepted) == reached_unconfined, "the confined ffprobe still connected"


def test_the_native_lane_refuses_a_command_that_is_not_its_declared_binary(tmp_path):
    """Fail closed on a shape it cannot fence, rather than run unfenced."""
    with pytest.raises(ValueError, match="exec_binary"):
        asyncio.run(
            run_sandboxed(
                ["/bin/echo", "hello"],
                policy=SandboxPolicy(
                    confine=fence.Confinement(op="test", exec_binary="/usr/bin/ffmpeg")
                ),
            )
        )


def test_a_confined_python_worker_must_be_a_managed_entrypoint():
    """The other half of failing closed: an unrecognised Python shape."""
    with pytest.raises(ValueError, match="managed Python entrypoint"):
        asyncio.run(
            run_sandboxed(
                # subprocess-boundary: seccomp/Landlock are per-process kernel state installed between fork and exec; no in-process invoker can exercise them
                [sys.executable, "-m", "json.tool"],
                policy=SandboxPolicy(confine=fence.Confinement(op="test")),
            )
        )


# --- the Python lane: markitdown and OpenCV --------------------------------


def _convert(path: Path, scratch: Path, *, confined: bool) -> dict:
    from frisket.runtime.launch import worker_argv

    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / "convert-result.json"
    profile = fence.Confinement(
        op="to_markdown (markitdown)",
        read=(str(path), "/etc/mime.types"),
        write=(str(scratch),),
    )
    result = asyncio.run(
        run_sandboxed(
            # subprocess-boundary: seccomp/Landlock are per-process kernel state installed between fork and exec; no in-process invoker can exercise them
            worker_argv("markitdown"),
            policy=SandboxPolicy(
                cpu_seconds=600,
                wall_seconds=900,
                memory_mb=4096,
                env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                confine=profile if confined else None,
            ),
            stdin_data=json.dumps({"path": str(path), "out": str(out)}).encode(),
        )
    )
    assert result.returncode == 0, result.stderr[-800:]
    return json.loads(out.read_text())


def test_the_real_to_markdown_conversion_is_unchanged_by_confinement(tmp_path):
    """The confined side is `ConvertMarkdownRecipe._convert_markitdown` itself.

    Same reason as the rasterization proof above: rebuilding the profile in
    the test would let the op's own profile rot unnoticed.
    """
    from tests.document_conversion_helpers import bound_document_converter

    plain = _convert(FIXTURE_PDF, tmp_path / "plain", confined=False)
    assert "markdown" in plain, plain
    assert plain["markdown"].strip(), "the unconfined conversion produced nothing"

    scratch = tmp_path / "confined"
    scratch.mkdir()
    confined = asyncio.run(
        bound_document_converter()._convert_markitdown(FIXTURE_PDF, scratch)
    )
    assert confined == plain["markdown"]


def test_a_confined_markitdown_cannot_read_the_operators_other_files(tmp_path):
    """`~/.frisket/secrets/master.key` is the file this is really about.

    A converter that reads one declared document has no reason to reach any
    other path, and until the fence there was nothing but convention stopping
    it: the child ran with the server user's whole filesystem.
    """
    secret = tmp_path / "master.key"
    secret.write_text("SENTINEL-4271-not-for-the-converter\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    leaked = _convert(secret, scratch, confined=False)
    assert "SENTINEL-4271" in leaked.get("markdown", ""), (
        "the unconfined converter did not read the file, so the confined "
        "comparison below would prove nothing"
    )

    from frisket.runtime.launch import worker_argv

    out = scratch / "confined.json"
    result = asyncio.run(
        run_sandboxed(
            # subprocess-boundary: seccomp/Landlock are per-process kernel state installed between fork and exec; no in-process invoker can exercise them
            worker_argv("markitdown"),
            policy=SandboxPolicy(
                wall_seconds=300,
                memory_mb=4096,
                env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                confine=fence.Confinement(
                    op="to_markdown (markitdown)",
                    read=(str(FIXTURE_PDF), "/etc/mime.types"),
                    write=(str(scratch),),
                ),
            ),
            stdin_data=json.dumps({"path": str(secret), "out": str(out)}).encode(),
        )
    )
    assert result.returncode == 0, result.stderr[-500:]
    answer = json.loads(out.read_text())
    assert "markdown" not in answer or "SENTINEL-4271" not in answer["markdown"]
    assert "Permission denied" in answer.get("error", ""), answer


def test_a_confined_face_worker_reads_its_image_and_writes_its_crops(tmp_path):
    """OpenCV's decoders, confined to one image in and one directory out."""
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    from frisket.runtime.launch import worker_argv

    image = tmp_path / "frame.png"
    cv2.imwrite(str(image), numpy.full((240, 320, 3), 200, dtype=numpy.uint8))
    undeclared = tmp_path / "elsewhere.png"
    shutil.copy(image, undeclared)

    def run(path: Path, *, confined: bool) -> dict:
        result = asyncio.run(
            run_sandboxed(
                # subprocess-boundary: seccomp/Landlock are per-process kernel state installed between fork and exec; no in-process invoker can exercise them
                worker_argv("faces"),
                policy=SandboxPolicy(
                    wall_seconds=120,
                    memory_mb=2048,
                    env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
                    confine=(
                        fence.Confinement(
                            op="media.extract_faces",
                            read=(str(image),),
                            write=(str(tmp_path / "out"),),
                        )
                        if confined
                        else None
                    ),
                ),
                stdin_data=json.dumps(
                    {
                        "path": str(path),
                        "model_path": str(
                            Path(__file__).parents[2]
                            / "src/frisket/data/face_detection/face_detection_yunet_2023mar.onnx"
                        ),
                        "out_dir": str(tmp_path / "out"),
                    }
                ).encode(),
            )
        )
        assert result.returncode == 0, result.stderr[-500:]
        return json.loads(result.stdout)

    (tmp_path / "out").mkdir()
    assert run(image, confined=True) == run(image, confined=False)
    # cv2.imread returns None rather than raising when the open is refused,
    # which the worker already reports as an unreadable image.
    assert run(undeclared, confined=True) == {"error": "unreadable image"}
    assert run(undeclared, confined=False)["faces"] == []


# --- the converter posture: degrade loudly on a kernel without a fence -----


def test_a_kernel_that_cannot_fence_degrades_loudly_instead_of_refusing(
    monkeypatch, caplog, tmp_path
):
    """The decided difference from the recipe lane, pinned.

    Refusing a code recipe on an unfenceable kernel costs one optional
    feature. Refusing document import would cost the product's core loop for
    that operator, and the state Frisket shipped in until now is exactly this
    unconfined one -- so degrading is a missed improvement where refusing
    would be a regression. The warning is what makes it not silent.
    """
    monkeypatch.setattr(
        fence, "kernel_can_confine", lambda: "landlock is unavailable on this kernel"
    )
    from tests.document_conversion_helpers import bound_document_converter

    monkeypatch.setattr(fence, "_DEGRADED_OPS", set())
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with caplog.at_level(logging.WARNING, logger="frisket.engine.sandbox.fence"):
        markdown = asyncio.run(
            bound_document_converter()._convert_markitdown(FIXTURE_PDF, scratch)
        )
    assert markdown.strip(), "the op refused instead of degrading"
    assert any(
        "runs UNCONFINED on this kernel" in record.message
        and "to_markdown (markitdown)" in str(record.args)
        for record in caplog.records
    ), caplog.records


def test_the_degradation_warning_is_said_once_per_op_not_once_per_process(monkeypatch):
    """Two ops on an unfenceable kernel are two different pieces of news."""
    monkeypatch.setattr(fence, "kernel_can_confine", lambda: "no landlock")
    monkeypatch.setattr(fence, "_DEGRADED_OPS", set())
    seen = []
    monkeypatch.setattr(
        fence.logger, "warning", lambda *args, **kwargs: seen.append(args[1])
    )
    for _ in range(2):
        fence.warn_confinement_unavailable(
            "ocr (pdftoppm rasterization)", "no landlock"
        )
        fence.warn_confinement_unavailable("media.video_frames (ffmpeg)", "no landlock")
    assert seen == ["ocr (pdftoppm rasterization)", "media.video_frames (ffmpeg)"]


# --- the ops declare it, and go red if they stop ---------------------------


def _captured_policy(monkeypatch, module, run):
    captured = {}

    async def capture(argv, **kwargs):
        captured.setdefault("calls", []).append((list(argv), kwargs.get("policy")))
        from frisket.engine.sandbox.shim import SandboxResult

        return SandboxResult(returncode=1, stdout="", stderr="captured")

    monkeypatch.setattr(module, "run_sandboxed", capture)
    try:
        run()
    except Exception:
        pass
    return captured.get("calls", [])


def test_every_media_op_declares_a_confinement_naming_its_input(monkeypatch, tmp_path):
    """A closure test over the four wired call sites.

    Enumeration is the failure mode this repo keeps paying for, so the check
    is mechanical: drive each op's real `execute`/`_page_images` far enough to
    reach `run_sandboxed`, and assert what it handed the shim. Dropping a
    `confine=` -- or forgetting the input path in a new one -- goes red here
    rather than silently un-fencing a converter.
    """
    from frisket.ops import ocr_engines as ocr_module
    from frisket.engine.executor import document_convert as markdown_module
    from tests.document_conversion_helpers import bound_document_converter

    document = tmp_path / "input.pdf"
    document.write_bytes(FIXTURE_PDF.read_bytes())
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    calls: list[tuple[list[str], SandboxPolicy]] = []
    calls += _captured_policy(
        monkeypatch,
        markdown_module,
        lambda: asyncio.run(
            bound_document_converter()._convert_markitdown(document, scratch)
        ),
    )
    if shutil.which("pdftoppm"):
        calls += _captured_policy(
            monkeypatch,
            ocr_module,
            lambda: asyncio.run(
                ocr_module.OcrEngines()._page_images(
                    document,
                    {"mime": "application/pdf"},
                    {"dpi": 50},
                    scratch,
                )
            ),
        )

    assert calls, "no op reached the sandbox"
    for argv, policy in calls:
        assert policy.confine is not None, f"{argv[0]} spawns unconfined"
        assert str(document) in policy.confine.read_paths(), (
            f"{policy.confine.op} does not declare its own input as readable"
        )


def test_every_sandbox_policy_in_the_media_pipeline_is_confined_or_listed_here():
    """The closure test: a NEW unconfined spawn in these modules goes red.

    Driving each op's `execute` would need a Project and a row, so the check
    that has to be mechanical is done on the source instead: every
    `SandboxPolicy(...)` built in the document/media modules either carries a
    `confine=` or appears below with its reason. Enumeration is the failure
    mode this repo keeps paying for, and undercounting is the direction it
    fails in -- so the exception list is asserted exactly, not merely
    contained.
    """
    import ast

    from frisket.ops import ocr_engines
    from frisket.engine.executor import document_convert
    from frisket.engine.executor import row_media_read
    from frisket.engine._workers import rapidocr_session

    # All currently enumerated media spawns now carry a derived Confinement,
    # including the optional tesseract path.
    expected_unconfined = set()
    found = set()
    for module in (document_convert, row_media_read, ocr_engines, rapidocr_session):
        # rule19: closure fence — unconfined SandboxPolicy spawns asserted exactly against the reviewed exception list
        tree = ast.parse(Path(module.__file__).read_text())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "SandboxPolicy"
            ):
                continue
            if any(keyword.arg == "confine" for keyword in node.keywords):
                continue
            enclosing = node
            while enclosing is not None and not isinstance(
                enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                enclosing = parents.get(enclosing)
            found.add(
                (
                    module.__name__.rsplit(".", 1)[-1],
                    enclosing.name if enclosing else "<module>",
                )
            )
    assert found == expected_unconfined, (
        "a document/media op spawns a sandbox child without a Confinement. "
        "Derive its profile by running it and observing, or add it here with "
        f"the reason. Found: {sorted(found)}"
    )


def test_the_rapidocr_session_declares_a_confinement_that_needs_no_input_rule():
    """The long-lived worker's inputs change per request; its ruleset cannot.

    That works only because `_stage_request` copies every page into the
    session's own staging root, which is the child's cwd and granted by
    construction. If staging ever stopped copying, this profile would be
    wrong -- so the property is asserted here rather than assumed.
    """
    from frisket.engine._workers.rapidocr_session import RapidOCRProcessSession

    session = RapidOCRProcessSession.__new__(RapidOCRProcessSession)
    session.model_root_dir = None
    profile = session._confinement()
    assert profile.exec_binary is None
    assert set(profile.read) == {"/proc/cpuinfo", "/sys/devices/system/cpu"}
    assert profile.write == ()

    session.model_root_dir = "/var/cache/frisket/rapidocr"
    assert "/var/cache/frisket/rapidocr" in session._confinement().read
