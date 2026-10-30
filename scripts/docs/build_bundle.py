#!/usr/bin/env python3
"""Join guide plans, app presentation and successful browser captures for the site."""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import shutil
import tempfile
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def unique(items: list[dict], label: str) -> dict[str, dict]:
    result = {}
    for item in items:
        identity = item["id"]
        if identity in result:
            raise ValueError(f"Duplicate {label}: {identity}")
        result[identity] = item
    return result


def build_bundle(
    *,
    plans: Path,
    catalog: Path,
    samples: Path,
    captures: list[Path],
    output: Path,
    app_revision: str,
) -> None:
    """Write a new bundle directory only after references and captures agree."""
    if not re.fullmatch(r"[0-9a-f]{40}", app_revision):
        raise ValueError("App revision must be a full lowercase Git SHA")
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output already exists: {output}")
    recipes = [read_json(path) for path in sorted(plans.glob("*.json"))]
    if not recipes:
        raise ValueError("No recipe plans")
    unique(recipes, "recipe")
    raw_catalog = read_json(catalog)
    if (
        raw_catalog.get("version") != 1
        or raw_catalog.get("appRevision") != app_revision
    ):
        raise ValueError("Catalog revision mismatch")
    actions = [
        {
            **action,
            "name": action["label"],
            "uiPath": [*action.get("path", []), action["label"]],
            "settings": action.get("params", []),
        }
        for action in raw_catalog["actions"]
    ]
    actions_by_id = unique(actions, "action")
    sample_records = read_json(samples)["samples"]
    samples_by_id = unique(sample_records, "sample")
    required_captures: dict[str, tuple[str, int]] = {}
    for recipe in recipes:
        sample = recipe["sample"]
        published_sample = samples_by_id.get(sample["id"])
        if (
            not published_sample
            or type(sample["revision"]) is not int
            or published_sample["revision"] != sample["revision"]
        ):
            raise ValueError(f"Unknown sample revision for {recipe['id']}")
        unique(recipe["steps"], f"step in {recipe['id']}")
        for step in recipe["steps"]:
            if step.get("actionId") and step["actionId"] not in actions_by_id:
                raise ValueError(f"Unknown user-facing action: {step['actionId']}")
            unique(step.get("settings", []), f"setting in {recipe['id']}/{step['id']}")
            for setting in step.get("settings", []):
                param = setting.get("param")
                if param is not None:
                    action = actions_by_id.get(step.get("actionId"))
                    properties = (
                        action.get("inputSchema", {}).get("properties", {})
                        if action
                        else {}
                    )
                    if param not in properties:
                        raise ValueError(
                            f"Unknown action parameter: {step.get('actionId')}/{param}"
                        )
            for identity in step.get("captureIds", []):
                expected_sample = (sample["id"], sample["revision"])
                if (
                    identity in required_captures
                    and required_captures[identity] != expected_sample
                ):
                    raise ValueError(
                        f"Capture {identity} reused across different samples"
                    )
                required_captures[identity] = expected_sample

    records = []
    image_copies: list[tuple[Path, str]] = []
    for capture_directory in captures:
        bundle = read_json(capture_directory / "bundle.json")
        if bundle["version"] != 1 or bundle["appRevision"] != app_revision:
            raise ValueError(f"Capture revision mismatch: {capture_directory}")
        sample = bundle["sample"]
        published_sample = samples_by_id.get(sample["id"])
        if (
            not published_sample
            or type(sample["revision"]) is not int
            or published_sample["revision"] != sample["revision"]
        ):
            raise ValueError(f"Unknown capture sample revision: {capture_directory}")
        for record in bundle["captures"]:
            identity = record["id"]
            if (
                identity in required_captures
                and (sample["id"], sample["revision"]) != required_captures[identity]
            ):
                raise ValueError(f"Capture sample mismatch: {identity}")
            image = (capture_directory / record["image"]).resolve()
            if (
                not image.is_relative_to(capture_directory.resolve())
                or not image.is_file()
            ):
                raise ValueError(f"Missing or invalid capture image: {identity}")
            if any(
                type(record[key]) is not int or record[key] <= 0
                for key in ("width", "height", "scale")
            ):
                raise ValueError(f"Invalid capture dimensions: {identity}")
            with image.open("rb") as stream:
                header = stream.read(24)
            if (
                len(header) != 24
                or header[:8] != b"\x89PNG\r\n\x1a\n"
                or header[12:16] != b"IHDR"
                or struct.unpack(">II", header[16:24])
                != (record["width"], record["height"])
            ):
                raise ValueError(f"Capture PNG dimensions mismatch: {identity}")
            unique(record.get("annotations", []), f"annotation in {identity}")
            for index, annotation in enumerate(record.get("annotations", []), start=1):
                if annotation["number"] != index:
                    raise ValueError(f"Invalid annotation order: {identity}")
                rect = annotation["rect"]
                if (
                    any(
                        type(rect[key]) not in (int, float)
                        or not math.isfinite(rect[key])
                        or not 0 <= rect[key] <= 1
                        for key in ("x", "y", "width", "height")
                    )
                    or rect["width"] <= 0
                    or rect["height"] <= 0
                    or rect["x"] + rect["width"] > 1.000001
                    or rect["y"] + rect["height"] > 1.000001
                ):
                    raise ValueError(f"Invalid annotation geometry: {identity}")
            # Capture IDs become filenames, so limit them to ordinary guide slugs.
            if not identity or any(
                c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in identity
            ):
                raise ValueError(f"Invalid capture ID: {identity}")
            relative = f"assets/docs/{app_revision[:12]}/{identity}.png"
            records.append(
                {
                    **record,
                    "image": "/" + relative,
                    "sample": sample,
                    **({"replay": bundle["replay"]} if bundle.get("replay") else {}),
                }
            )
            image_copies.append((image, relative))
    captures_by_id = unique(records, "capture")
    missing = required_captures.keys() - captures_by_id.keys()
    if missing:
        raise ValueError(f"Missing recipe captures: {', '.join(sorted(missing))}")

    for recipe in recipes:
        for step in recipe["steps"]:
            annotation_ids = {
                annotation["id"]
                for capture_id in step.get("captureIds", [])
                for annotation in captures_by_id[capture_id].get("annotations", [])
            }
            for setting in step.get("settings", []):
                if (
                    setting.get("target") is not None
                    and setting["id"] not in annotation_ids
                ):
                    raise ValueError(
                        f"Missing capture annotation for {recipe['id']}/{step['id']}/{setting['id']}"
                    )

    result = {
        "version": 1,
        "appRevision": app_revision,
        "samples": sample_records,
        "recipes": recipes,
        "actions": actions,
        "captures": records,
        "engines": raw_catalog.get("engines", []),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        for source, relative in image_copies:
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        (stage / "guideBundle.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if output.exists() or output.is_symlink():
            raise ValueError(f"Output already exists: {output}")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plans", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--captures", type=Path, action="append", required=True)
    parser.add_argument("--app-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_bundle(**vars(args))


if __name__ == "__main__":
    main()
