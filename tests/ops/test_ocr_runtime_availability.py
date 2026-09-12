"""Non-skipping acceptance for RapidOCR runtime truth and sandbox parity.

The ordinary source-checkout node is deliberately dependency-light.  The two
image nodes are release-proof gates: ``FRISKET_OCR_SUPPORTED_IMAGE`` must name
an immutable image that is already present in the local Docker daemon.  They do
not build, pull, or skip when the designated environment is unavailable.
"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
import types
from pathlib import Path
from typing import Any, NoReturn

import pytest


pytestmark = pytest.mark.gap

SUPPORTED_IMAGE_ENV = "FRISKET_OCR_SUPPORTED_IMAGE"
EXPECTED_SOURCE_SHA_ENV = "FRISKET_OCR_EXPECTED_SOURCE_SHA"
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_REFERENCE = re.compile(r"^(?!-)[^@\s]+@sha256:[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_RESULT_PREFIX = "FRISKET_OCR_ACCEPTANCE_RESULT="
_IMAGE_TIMEOUT_SECONDS = 360


def _fail(code: str, detail: str) -> NoReturn:
    raise AssertionError(f"[{code}] {detail}")


def _bounded_output(value: str, limit: int = 4_000) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[-limit:]


def _canonical_whitespace(value: str) -> str:
    return " ".join(value.split())


def _supported_image_id() -> tuple[str, str]:
    reference = os.environ.get(SUPPORTED_IMAGE_ENV, "").strip()
    if not reference:
        _fail(
            "OCR_SUPPORTED_IMAGE_NOT_CONFIGURED",
            f"set {SUPPORTED_IMAGE_ENV} to a locally present immutable image "
            "ID or digest; this proof must not skip",
        )
    if not (_IMAGE_ID.fullmatch(reference) or _DIGEST_REFERENCE.fullmatch(reference)):
        _fail(
            "OCR_SUPPORTED_IMAGE_NOT_IMMUTABLE",
            f"{SUPPORTED_IMAGE_ENV} must be sha256:<64 hex> or "
            "name@sha256:<64 hex>; mutable tags are not release evidence",
        )

    docker = shutil.which("docker")
    if docker is None:
        _fail(
            "OCR_DOCKER_UNAVAILABLE",
            "the designated supported-image gate requires the Docker client",
        )
    try:
        inspected = subprocess.run(
            [docker, "image", "inspect", "--format", "{{.Id}}", reference],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(
            "OCR_DOCKER_UNAVAILABLE",
            f"could not inspect the locally designated image: {type(exc).__name__}",
        )
    if inspected.returncode != 0:
        detail = _bounded_output(inspected.stderr or inspected.stdout)
        _fail(
            "OCR_SUPPORTED_IMAGE_NOT_LOCAL",
            "the immutable image is not inspectable in the local daemon; "
            f"build/load it before this gate ({detail or 'no Docker diagnostic'})",
        )
    image_id = inspected.stdout.strip()
    if not _IMAGE_ID.fullmatch(image_id):
        _fail(
            "OCR_SUPPORTED_IMAGE_ID_INVALID",
            f"Docker returned a non-immutable image ID: {image_id!r}",
        )
    return docker, image_id


def _expected_current_source_sha() -> str:
    expected = os.environ.get(EXPECTED_SOURCE_SHA_ENV, "").strip()
    if not expected:
        _fail(
            "OCR_EXPECTED_SOURCE_SHA_NOT_CONFIGURED",
            f"set {EXPECTED_SOURCE_SHA_ENV} to the full current checkout commit",
        )
    if not _SOURCE_SHA.fullmatch(expected):
        _fail(
            "OCR_EXPECTED_SOURCE_SHA_INVALID",
            f"{EXPECTED_SOURCE_SHA_ENV} must be a full 40-character Git SHA",
        )
    git = shutil.which("git")
    if git is None:
        _fail("OCR_CURRENT_SOURCE_SHA_UNAVAILABLE", "git is required for image binding")
    try:
        current = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _fail(
            "OCR_CURRENT_SOURCE_SHA_UNAVAILABLE",
            f"could not resolve the checkout commit: {type(exc).__name__}",
        )
    actual = current.stdout.strip()
    if current.returncode != 0 or not _SOURCE_SHA.fullmatch(actual):
        _fail(
            "OCR_CURRENT_SOURCE_SHA_UNAVAILABLE",
            _bounded_output(current.stderr or current.stdout) or "git returned no SHA",
        )
    if actual != expected:
        _fail(
            "OCR_EXPECTED_SOURCE_SHA_STALE",
            f"configured source SHA {expected} does not match checkout HEAD {actual}",
        )
    return expected


def _run_supported_image(script: str, *, exercise_ambient_env: bool) -> dict[str, Any]:
    docker, image_id = _supported_image_id()
    expected_source_sha = _expected_current_source_sha()
    encoded = base64.b64encode(textwrap.dedent(script).encode()).decode()
    wrapper = (
        "import base64, signal; "
        f"signal.alarm({_IMAGE_TIMEOUT_SECONDS - 30}); "
        f"exec(compile(base64.b64decode({encoded!r}), '<ocr-acceptance>', 'exec'))"
    )
    command = [
        docker,
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,size=1g",
        "--env",
        f"{EXPECTED_SOURCE_SHA_ENV}={expected_source_sha}",
    ]
    if exercise_ambient_env:
        # Controlled sentinels, never host values.  The parent must still run,
        # while the sandboxed child must not inherit these ambient paths/keys.
        command.extend(
            [
                "--env",
                "HOME=/tmp/frisket-ocr-ambient-home",
                "--env",
                "PYTHONPATH=/tmp/frisket-ocr-ambient-pythonpath",
                "--env",
                "VIRTUAL_ENV=/tmp/frisket-ocr-ambient-venv",
                "--env",
                "OPENAI_API_KEY=stage0a-controlled-openai-sentinel",
                "--env",
                "FRISKET_OCR_AMBIENT_SECRET=stage0a-controlled-frisket-sentinel",
            ]
        )
    command.extend([image_id, "/app/.venv/bin/python", "-c", wrapper])

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_IMAGE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _fail(
            "OCR_SUPPORTED_IMAGE_TIMEOUT",
            f"bounded image proof exceeded {_IMAGE_TIMEOUT_SECONDS} seconds",
        )
    except OSError as exc:
        _fail(
            "OCR_DOCKER_UNAVAILABLE",
            f"could not execute the designated image: {type(exc).__name__}",
        )
    if completed.returncode != 0:
        stdout = _bounded_output(completed.stdout)
        stderr = _bounded_output(completed.stderr)
        _fail(
            "OCR_SUPPORTED_IMAGE_EXECUTION_FAILED",
            "RapidOCR/PDFium/image acceptance failed without skipping; "
            f"stdout={stdout!r} stderr={stderr!r}",
        )

    payload_line = next(
        (
            line[len(_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(_RESULT_PREFIX)
        ),
        None,
    )
    if payload_line is None:
        _fail(
            "OCR_SUPPORTED_IMAGE_RESULT_MISSING",
            "the image command exited successfully without its acceptance result",
        )
    try:
        payload = json.loads(payload_line)
    except json.JSONDecodeError as exc:
        _fail("OCR_SUPPORTED_IMAGE_RESULT_INVALID", str(exc))
    if not isinstance(payload, dict):
        _fail("OCR_SUPPORTED_IMAGE_RESULT_INVALID", "result must be a JSON object")
    payload["inspected_image_id"] = image_id
    if payload.get("source_identity") != expected_source_sha:
        _fail(
            "OCR_SUPPORTED_IMAGE_SOURCE_IDENTITY_MISMATCH",
            "the immutable image does not expose the expected current source SHA",
        )
    return payload


_EXACT_IMPORT_AND_SMOKE = r"""
import json
import os
import sys
from importlib import metadata
from pathlib import Path

import cv2
import numpy as np
import pypdfium2
import rapidocr
from rapidocr import RapidOCR
from rapidocr.utils.typings import OCRVersion
from frisket.engine.worker_version import code_version

PREFIX = "FRISKET_OCR_ACCEPTANCE_RESULT="
expected_source_sha = os.environ["FRISKET_OCR_EXPECTED_SOURCE_SHA"]
source_identity = code_version()
assert source_identity != "unknown", "supported image exposes unknown source identity"
assert source_identity == expected_source_sha, (
    "supported image source identity does not match the expected checkout commit"
)
assert metadata.version("pypdfium2") == pypdfium2.PYPDFIUM_INFO, (
    "supported image has an inconsistent PDFium package"
)

canvas = np.full((360, 1600, 3), 255, dtype=np.uint8)
cv2.putText(
    canvas,
    "FRISKET OCR 4271",
    (45, 225),
    cv2.FONT_HERSHEY_SIMPLEX,
    3.0,
    (0, 0, 0),
    7,
    cv2.LINE_AA,
)
result = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5})(canvas)
texts = [str(value).strip() for value in (getattr(result, "txts", None) or [])]
joined = " ".join(value for value in texts if value).strip()
assert joined, "exact RapidOCR class produced no text in the bounded real smoke"
normalized = "".join(character for character in joined.upper() if character.isalnum())
assert "FRISKET" in normalized and "4271" in normalized, (
    "exact RapidOCR class did not recognize the bounded smoke sentinel"
)

print(
    PREFIX
    + json.dumps(
        {
            "executable": str(Path(sys.executable).resolve()),
            "pypdfium2_version": metadata.version("pypdfium2"),
            "rapidocr_class_module": RapidOCR.__module__,
            "rapidocr_file": str(Path(rapidocr.__file__).resolve()),
            "rapidocr_version": metadata.version("rapidocr"),
            "source_identity": source_identity,
            "text": joined,
        },
        sort_keys=True,
    )
)
"""


_PARENT_AND_SANDBOX_GOLDEN = r"""
import asyncio
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
from importlib import metadata
from pathlib import Path

import cv2
import numpy as np
import rapidocr
from PIL import Image
from pypdf import PdfReader
from rapidocr import RapidOCR

import frisket.ops.ocr_engines as ocr_module
from frisket.engine.sandbox import shim as sandbox_shim
from frisket.ops.ocr_engines import OcrEngines
from frisket.ops.searchable_pdf import compose_searchable_pdf
from frisket.engine.worker_version import code_version

PREFIX = "FRISKET_OCR_ACCEPTANCE_RESULT="
AMBIENT_HOME = "/tmp/frisket-ocr-ambient-home"
AMBIENT_PYTHONPATH = "/tmp/frisket-ocr-ambient-pythonpath"
AMBIENT_VIRTUAL_ENV = "/tmp/frisket-ocr-ambient-venv"


def package_fingerprint():
    packages = sorted(
        (
            (distribution.metadata.get("Name") or "").casefold().replace("_", "-"),
            distribution.version,
        )
        for distribution in metadata.distributions()
    )
    encoded = json.dumps(packages, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), len(packages)


def normalize_rapidocr(result):
    texts = list(getattr(result, "txts", None) or [])
    boxes = getattr(result, "boxes", None)
    scores = list(getattr(result, "scores", None) or [])
    blocks = []
    for index, text in enumerate(texts):
        block = {"text": text}
        if boxes is not None and index < len(boxes):
            block["bbox"] = [[int(x), int(y)] for x, y in boxes[index]]
        if index < len(scores):
            block["score"] = round(float(scores[index]), 4)
        blocks.append(block)
    return {
        "text": " ".join(str(text).strip() for text in texts).strip(),
        "blocks": blocks,
    }


def canonical_text(value):
    return " ".join(str(value).split())


def assert_semantic_page_parity(parent_pages, child_pages):
    assert len(parent_pages) == len(child_pages), (
        "parent and actual sandbox OCR_WORKER produced different page counts"
    )
    for parent_page, child_page in zip(parent_pages, child_pages, strict=True):
        assert set(parent_page) == set(child_page) == {"text", "blocks"}, (
            "parent and sandbox OCR page shapes differ"
        )
        assert canonical_text(parent_page["text"]) == canonical_text(
            child_page["text"]
        ), "parent and sandbox OCR page text differs beyond whitespace"
        parent_blocks = parent_page["blocks"]
        child_blocks = child_page["blocks"]
        assert len(parent_blocks) == len(child_blocks), (
            "parent and sandbox OCR block counts differ"
        )
        for parent_block, child_block in zip(
            parent_blocks, child_blocks, strict=True
        ):
            assert set(parent_block) == set(child_block) == {
                "text",
                "bbox",
                "score",
            }, (
                "parent and sandbox OCR block shapes differ"
            )
            assert canonical_text(parent_block["text"]) == canonical_text(
                child_block["text"]
            ), "parent and sandbox OCR block text differs beyond whitespace"
            assert parent_block["bbox"] == child_block["bbox"], (
                "parent and sandbox OCR bounding boxes differ"
            )
            assert parent_block["score"] == child_block["score"], (
                "parent and sandbox OCR scores differ"
            )


def searchable_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return " ".join(
        word
        for page in reader.pages
        for word in (page.extract_text() or "").split()
    ).strip()


def json_line(stdout):
    for line in reversed(stdout.splitlines()):
        if line.startswith(PREFIX):
            return json.loads(line[len(PREFIX) :])
    raise AssertionError("sandbox metadata probe returned no acceptance result")


async def run():
    assert os.environ.get("HOME") == AMBIENT_HOME, "controlled parent HOME missing"
    assert os.environ.get("PYTHONPATH") == AMBIENT_PYTHONPATH, (
        "controlled parent PYTHONPATH missing"
    )
    assert os.environ.get("VIRTUAL_ENV") == AMBIENT_VIRTUAL_ENV, (
        "controlled parent VIRTUAL_ENV missing"
    )
    import pypdfium2

    assert metadata.version("pypdfium2") == pypdfium2.PYPDFIUM_INFO, (
        "supported image has an inconsistent PDFium package"
    )
    Path(AMBIENT_HOME).mkdir(parents=True, exist_ok=True)
    expected_source_sha = os.environ.get("FRISKET_OCR_EXPECTED_SOURCE_SHA")
    source_identity = code_version() if expected_source_sha else None
    if expected_source_sha:
        assert source_identity != "unknown", (
            "supported image exposes unknown source identity"
        )
        assert source_identity == expected_source_sha, (
            "supported image source identity does not match the expected checkout commit"
        )

    # A deterministic raster-only PDF makes PDFium, the exact parent engine,
    # the actual sandboxed OCR_WORKER, and the searchable-PDF composer all
    # participate in one bounded golden.
    canvas = np.full((360, 1600, 3), 255, dtype=np.uint8)
    cv2.putText(
        canvas,
        "FRISKET OCR 4271",
        (45, 225),
        cv2.FONT_HERSHEY_SIMPLEX,
        3.0,
        (0, 0, 0),
        7,
        cv2.LINE_AA,
    )
    pdf_buffer = io.BytesIO()
    Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)).save(
        pdf_buffer, format="PDF", resolution=200.0
    )
    source_pdf = pdf_buffer.getvalue()

    root = Path(tempfile.mkdtemp(prefix="frisket-ocr-image-proof-"))
    try:
        source_path = root / "golden.pdf"
        source_path.write_bytes(source_pdf)
        scratch = root / "scratch"
        scratch.mkdir()
        recipe = OcrEngines()
        pages = await recipe._page_images(
            source_path,
            {"mime": "application/pdf", "filename": "golden.pdf"},
            {"dpi": 200},
            scratch,
        )
        assert pages, "PDFium rasterization produced no pages"

        from rapidocr.utils.typings import OCRVersion
        parent_engine = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5})
        parent_pages = [normalize_rapidocr(parent_engine(str(page))) for page in pages]
        assert any(page["text"].strip() for page in parent_pages), (
            "exact parent RapidOCR class produced no golden text"
        )
        parent_text = " ".join(page["text"] for page in parent_pages)
        normalized_parent_text = "".join(
            character for character in parent_text.upper() if character.isalnum()
        )
        assert "FRISKET" in normalized_parent_text and "4271" in normalized_parent_text, (
            "exact parent RapidOCR class did not recognize the golden sentinel"
        )

        # RapidOCR runs on a persistent sandboxed session, so the launch to
        # capture is the session's open_sandboxed_process, not a one-shot
        # run_sandboxed. Probes below re-enter the sandbox through the one-shot
        # door with the session's own argv and policy.
        run_sandboxed = ocr_module.run_sandboxed
        real_open_sandboxed_process = sandbox_shim.open_sandboxed_process
        captured = {}

        async def capture_open(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["policy"] = kwargs.get("policy")
            return await real_open_sandboxed_process(argv, **kwargs)

        sandbox_shim.open_sandboxed_process = capture_open
        try:
            async with ocr_module.rapidocr_execution_scope(expected_rows=1, language=None) as pool:
                child_pages = await OcrEngines(pool=pool)._ocr_rapidocr(pages, scratch)
        finally:
            sandbox_shim.open_sandboxed_process = real_open_sandboxed_process

        assert_semantic_page_parity(parent_pages, child_pages)
        parent_pdf, _ = compose_searchable_pdf(source_pdf, parent_pages, dpi=200)
        child_pdf, _ = compose_searchable_pdf(source_pdf, child_pages, dpi=200)
        parent_layer = searchable_text(parent_pdf)
        child_layer = searchable_text(child_pdf)
        assert parent_layer, "parent searchable-PDF golden has no text layer"
        assert canonical_text(parent_layer) == canonical_text(child_layer), (
            "parent and sandbox searchable-PDF text layers differ"
        )

        argv = captured.get("argv")
        policy = captured.get("policy")
        assert argv and policy is not None, "actual OCR worker launch was not captured"
        assert os.path.isabs(argv[0]), "OCR child interpreter argv is not absolute"

        probe = r'''\
import hashlib
import json
import os
import sys
from importlib import metadata
from pathlib import Path

import rapidocr
from rapidocr import RapidOCR

PREFIX = "FRISKET_OCR_ACCEPTANCE_RESULT="
SENTINELS = {
    "stage0a-controlled-openai-sentinel",
    "stage0a-controlled-frisket-sentinel",
}
packages = sorted(
    (
        (distribution.metadata.get("Name") or "").casefold().replace("_", "-"),
        distribution.version,
    )
    for distribution in metadata.distributions()
)
fingerprint = hashlib.sha256(
    json.dumps(packages, separators=(",", ":")).encode()
).hexdigest()
leaked_keys = sorted(
    key for key, value in os.environ.items() if value in SENTINELS
)
print(
    PREFIX
    + json.dumps(
        {
            "executable": str(Path(sys.executable).resolve()),
            "home_is_ambient": os.environ.get("HOME")
            == "/tmp/frisket-ocr-ambient-home",
            "pythonpath_is_ambient": os.environ.get("PYTHONPATH")
            == "/tmp/frisket-ocr-ambient-pythonpath",
            "virtual_env_is_ambient": os.environ.get("VIRTUAL_ENV")
            == "/tmp/frisket-ocr-ambient-venv",
            "leaked_secret_keys": leaked_keys,
            "package_count": len(packages),
            "package_fingerprint": fingerprint,
            "rapidocr_class_module": RapidOCR.__module__,
            "rapidocr_file": str(Path(rapidocr.__file__).resolve()),
            "rapidocr_version": metadata.version("rapidocr"),
        },
        sort_keys=True,
    )
)
'''
        probe_result = await run_sandboxed(
            [argv[0], "-c", probe],
            policy=policy,
        )
        assert probe_result.ok, "sandbox interpreter/package probe failed"
        child_runtime = json_line(probe_result.stdout)

        parent_fingerprint, parent_package_count = package_fingerprint()
        parent_executable = str(Path(sys.executable).resolve())
        parent_rapidocr_file = str(Path(rapidocr.__file__).resolve())
        assert str(Path(argv[0]).resolve()) == parent_executable, (
            "actual OCR child argv does not resolve to the parent interpreter"
        )
        assert child_runtime["executable"] == parent_executable, (
            "sandbox child resolved a different interpreter"
        )
        assert child_runtime["package_fingerprint"] == parent_fingerprint, (
            "sandbox child observes a different installed package set"
        )
        assert child_runtime["package_count"] == parent_package_count, (
            "sandbox child package count differs from parent"
        )
        assert child_runtime["rapidocr_file"] == parent_rapidocr_file, (
            "sandbox child imported RapidOCR from a different package path"
        )
        assert child_runtime["rapidocr_version"] == metadata.version("rapidocr"), (
            "sandbox child imported a different RapidOCR distribution version"
        )

        # Exercise the actual OCR worker policy, not merely the outer Docker
        # network namespace.  The parent proves this loopback listener is live;
        # a child using the captured allow_network=False policy must receive an
        # explicit PermissionError rather than connect to it.
        assert policy.allow_network is False, "actual OCR policy allows network"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.settimeout(2)
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            address = listener.getsockname()
            with socket.create_connection(address, timeout=2):
                control_connection, _ = listener.accept()
                control_connection.close()
            network_probe = r'''\
import json
import socket

PREFIX = "FRISKET_OCR_ACCEPTANCE_RESULT="
connected = False
error = None
try:
    with socket.create_connection(("127.0.0.1", __PORT__), timeout=2):
        connected = True
except Exception as exc:
    error = type(exc).__name__
print(PREFIX + json.dumps({"connected": connected, "error": error}))
'''.replace("__PORT__", str(address[1]))
            network_result = await run_sandboxed(
                [argv[0], "-c", network_probe],
                policy=policy,
            )
            assert network_result.ok, "sandbox loopback-denial probe failed to execute"
            network_observation = json_line(network_result.stdout)
            assert network_observation == {
                "connected": False,
                "error": "PermissionError",
            }, "actual OCR sandbox policy did not deny a reachable loopback socket"

        # These checks come after a successful real child golden.  A failure is
        # therefore a reproduced propagation defect, not a guessed launcher fix.
        assert not child_runtime["home_is_ambient"], (
            "sandbox inherited the parent HOME instead of a child-owned home"
        )
        assert not child_runtime["pythonpath_is_ambient"], (
            "sandbox inherited ambient PYTHONPATH"
        )
        assert not child_runtime["virtual_env_is_ambient"], (
            "sandbox inherited ambient VIRTUAL_ENV"
        )
        assert not child_runtime["leaked_secret_keys"], (
            "sandbox inherited controlled ambient secret sentinels"
        )
        assert not ({"HOME", "PYTHONPATH", "VIRTUAL_ENV"} & set(policy.env_passthrough)), (
            "OCR policy explicitly requests broad ambient Python/home inheritance"
        )

        return {
            "child_executable": child_runtime["executable"],
            "golden_text": child_layer,
            "package_fingerprint": parent_fingerprint,
            "parent_executable": parent_executable,
            "rapidocr_file": parent_rapidocr_file,
            "rapidocr_version": metadata.version("rapidocr"),
            "sandbox_network_error": network_observation["error"],
            "source_identity": source_identity,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


print(PREFIX + json.dumps(asyncio.run(run()), sort_keys=True))
"""


@pytest.mark.parametrize(
    ("parent_text", "child_text"),
    (
        ("FRISKET OCR 4271", "FRISKET\nOCR 4271"),
        ("FRISKET  OCR\t4271", " FRISKET OCR 4271 "),
    ),
)
def test_ocr_golden_equivalence_canonicalizes_only_whitespace(
    parent_text: str,
    child_text: str,
) -> None:
    assert _canonical_whitespace(parent_text) == _canonical_whitespace(child_text)
    assert _canonical_whitespace("FRISKET OCR 4271") != _canonical_whitespace(
        "FRISKET OCR 4272"
    )


def test_damaged_base_install_is_honestly_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace named rapidocr is not the exact worker dependency."""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    )

    def requirement_names(requirements: list[str]) -> set[str]:
        return {
            re.split(r"[\s\[<>=!~;]", requirement.strip(), maxsplit=1)[0].casefold()
            for requirement in requirements
        }

    base_names = requirement_names(pyproject["project"]["dependencies"])
    assert "rapidocr" in base_names

    namespace_only = types.ModuleType("rapidocr")
    namespace_only.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr", namespace_only)

    real_version = importlib.metadata.version
    real_distribution = importlib.metadata.distribution

    def no_rapidocr_distribution(name: str) -> str:
        if name.casefold() == "rapidocr":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_version(name)

    def no_rapidocr_distribution_object(name: str) -> importlib.metadata.Distribution:
        if name.casefold() == "rapidocr":
            raise importlib.metadata.PackageNotFoundError(name)
        return real_distribution(name)

    monkeypatch.setattr(importlib.metadata, "version", no_rapidocr_distribution)
    monkeypatch.setattr(
        importlib.metadata,
        "distribution",
        no_rapidocr_distribution_object,
    )

    import frisket.ops.ocr_engines as ocr_module
    from frisket.server.action_catalog_hints import _recipe_engines

    assert __import__("rapidocr") is namespace_only
    with pytest.raises(ImportError):
        exec("from rapidocr import RapidOCR", {})

    probe = getattr(ocr_module, "rapidocr_available", None)
    assert callable(probe), (
        "OCR availability needs one shared exact-class probe; namespace import "
        "or a hard-coded catalog availability flag is not runtime truth"
    )
    cache_clear = getattr(probe, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()
    available, error = probe()
    assert available is False
    assert error and "RapidOCR" in error and "ocr" in error.casefold()

    engines = {engine["id"]: engine for engine in _recipe_engines("media.ocr", {})}
    assert engines["rapidocr"]["available"] is False
    assert "RapidOCR" in engines["rapidocr"].get("error", "")


@pytest.mark.gap_env
def test_supported_image_exact_import_and_bounded_ocr_smoke_cannot_skip() -> None:
    result = _run_supported_image(_EXACT_IMPORT_AND_SMOKE, exercise_ambient_env=False)
    assert _IMAGE_ID.fullmatch(result["inspected_image_id"])
    assert result["rapidocr_class_module"].startswith("rapidocr")
    assert result["rapidocr_file"]
    assert result["rapidocr_version"]
    assert result["pypdfium2_version"] == "5.9.0"
    assert result["text"].strip()


@pytest.mark.gap_env
def test_supported_image_parent_and_sandbox_execute_same_golden_without_skip() -> None:
    result = _run_supported_image(
        _PARENT_AND_SANDBOX_GOLDEN,
        exercise_ambient_env=True,
    )
    assert result["child_executable"] == result["parent_executable"]
    assert result["rapidocr_file"]
    assert result["rapidocr_version"]
    assert result["package_fingerprint"]
    assert result["golden_text"].strip()
