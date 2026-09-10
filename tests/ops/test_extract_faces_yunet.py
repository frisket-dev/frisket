from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed
from frisket.engine.executor.row_media_read import FACE_WORKER, _YUNET_MODEL_PATH


FIXTURE = Path(__file__).parents[1] / "fixtures" / "face_detection" / "astronaut.png"


def _detect(image_path: Path, output_dir: Path) -> list[dict[str, int | str]]:
    import sys

    result = asyncio.run(
        run_sandboxed(
            [
                # subprocess-boundary: exercise the confined YuNet worker's stdin and crop-file outputs.
                sys.executable,
                "-c",
                FACE_WORKER + "\nassert cv2.getNumThreads() == 1, cv2.getNumThreads()",
            ],
            policy=SandboxPolicy(
                wall_seconds=120,
                memory_mb=2048,
                env_passthrough=["VIRTUAL_ENV", "PYTHONPATH"],
            ),
            stdin_data=json.dumps(
                {
                    "path": str(image_path),
                    "model_path": str(_YUNET_MODEL_PATH),
                    "out_dir": str(output_dir),
                }
            ).encode(),
        )
    )
    assert result.ok, result
    output = json.loads(result.stdout)
    assert "error" not in output, output
    return output["faces"]


def _assert_valid_faces(
    faces: list[dict[str, int | str]], *, width: int, height: int
) -> None:
    cv2 = pytest.importorskip("cv2")
    assert faces
    for face in faces:
        x, y, w, h = (face[key] for key in ("x", "y", "w", "h"))
        assert all(isinstance(value, int) for value in (x, y, w, h))
        assert 0 <= x < width
        assert 0 <= y < height
        assert 0 < w <= width - x
        assert 0 < h <= height - y

        pad = int(0.15 * w)
        crop = cv2.imread(str(face["crop"]))
        assert crop is not None
        assert crop.shape[:2] == (
            min(height, y + h + pad) - max(0, y - pad),
            min(width, x + w + pad) - max(0, x - pad),
        )


def test_yunet_detects_frontal_rotated_small_and_multiple_faces(tmp_path) -> None:
    cv2 = pytest.importorskip("cv2")
    source = cv2.imread(str(FIXTURE))
    assert source is not None

    cases: dict[str, tuple[object, int]] = {"frontal": (source, 1)}
    rotation = cv2.getRotationMatrix2D((256, 256), 20, 1)
    cases["rotated"] = (
        cv2.warpAffine(source, rotation, (512, 512), borderValue=(255, 255, 255)),
        1,
    )
    cases["small"] = (cv2.resize(source, (160, 160), interpolation=cv2.INTER_AREA), 1)
    half = cv2.resize(source, (256, 256), interpolation=cv2.INTER_AREA)
    cases["multiple"] = (cv2.hconcat([half, half]), 2)

    for name, (image, expected_faces) in cases.items():
        image_path = tmp_path / f"{name}.png"
        output_dir = tmp_path / name
        output_dir.mkdir()
        assert cv2.imwrite(str(image_path), image)
        faces = _detect(image_path, output_dir)
        assert len(faces) >= expected_faces
        _assert_valid_faces(faces, width=image.shape[1], height=image.shape[0])


def test_yunet_returns_no_faces_for_a_flat_image(tmp_path) -> None:
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    image_path = tmp_path / "flat.png"
    assert cv2.imwrite(
        str(image_path), numpy.full((240, 320, 3), 200, dtype=numpy.uint8)
    )

    output_dir = tmp_path / "crops"
    output_dir.mkdir()
    assert _detect(image_path, output_dir) == []
