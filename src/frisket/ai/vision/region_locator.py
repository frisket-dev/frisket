"""Action-neutral semantic image region location.

Generative vision models do not share one coordinate dialect: some emit XYXY,
others YXYX; some use pixels, 0..1, or 0..1000.  This module keeps that
irreducibly model-specific behavior behind one small interface and returns the
same strict page-normalized region contract to every consumer.

The first consumer is ``map.find``.  Nothing here imports or understands Find,
sheets, rows, citations, CAS, or publication.  Dedicated detectors such as
Florence-2 or Grounding DINO can implement :class:`RegionLocator` later without
changing consumers or leaking their library-specific result types.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from PIL import Image, ImageOps

from frisket.ai.llm.structured import StructuredCompleter, StructuredRequest
from frisket.ai.llm.types import ReasoningPolicy
from frisket.engine.store.grounding import normalize_bbox


CoordinateOrder = Literal["xyxy", "yxyx"]
CoordinateSpace = Literal["pixel", "normalized", "normalized_1000"]


@dataclass(frozen=True)
class ImageAsset:
    """Immutable decoded-image input for a region locator."""

    data: bytes
    media_type: str
    width: int
    height: int
    sha256: str
    display_width: int | None = None
    display_height: int | None = None

    def __post_init__(self) -> None:
        if not self.data:
            raise ValueError("image data must not be empty")
        if not self.media_type.startswith("image/"):
            raise ValueError("region location requires an image media type")
        if self.width < 1 or self.height < 1:
            raise ValueError("image dimensions must be positive")
        if not self.sha256:
            raise ValueError("image sha256 must not be empty")
        if self.display_width is None:
            object.__setattr__(self, "display_width", self.width)
        if self.display_height is None:
            object.__setattr__(self, "display_height", self.height)
        if self.display_width < 1 or self.display_height < 1:
            raise ValueError("display image dimensions must be positive")


@dataclass(frozen=True)
class LocateRegionsRequest:
    """One semantic region-location request, independent of any action."""

    engine_ref: str
    image: ImageAsset
    query: str
    detail_schema: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.engine_ref.strip():
            raise ValueError("engine_ref must not be blank")
        if not self.query.strip():
            raise ValueError("region query must not be blank")


@dataclass(frozen=True)
class RegionLocatorProfile:
    """A tested hosted-VLM grounding dialect.

    Coordinate order and space are explicit because their numeric ranges are
    ambiguous on roughly 1000-pixel images.  They are never inferred from the
    model output.
    """

    profile_id: str
    model_patterns: tuple[str, ...]
    bbox_field: str
    coordinate_order: CoordinateOrder
    coordinate_space: CoordinateSpace
    prompt_version: str
    reasoning_policy: ReasoningPolicy | None
    supports_detail_schema: bool = True


@dataclass(frozen=True)
class RegionProvenance:
    engine_ref: str
    profile_id: str
    prompt_version: str
    grounding_method: str = "vision_model_region"


@dataclass(frozen=True)
class LocatedRegion:
    match: str
    bbox: dict[str, Any]
    details: dict[str, Any]
    raw_bbox: tuple[float, float, float, float]
    provenance: RegionProvenance


@dataclass(frozen=True)
class RegionIssue:
    code: str
    message: str
    item_index: int | None = None


@dataclass(frozen=True)
class LocateRegionsResult:
    regions: tuple[LocatedRegion, ...]
    issues: tuple[RegionIssue, ...] = ()
    wire_calls: tuple[Any, ...] = ()


class RegionLocator(Protocol):
    """Backend-neutral semantic region locator."""

    supports_detail_schema: bool

    async def locate(self, request: LocateRegionsRequest) -> LocateRegionsResult: ...


class UnsupportedRegionLocator(ValueError):
    """The requested engine has no tested region-location profile."""


def prepare_image_asset(
    data: bytes,
    *,
    source_media_type: str,
    max_edge: int = 1024,
) -> ImageAsset:
    """Decode and deterministically bound an image for locator inference.

    EXIF orientation is applied before measuring display dimensions.  The
    inference copy is then scaled proportionally and encoded as PNG.  Because
    locators return normalized boxes, consumers can overlay the result on the
    original displayed image without persisting this bounded derivative.
    """

    if not data or not source_media_type.startswith("image/"):
        raise ValueError("region location requires non-empty image bytes")
    if max_edge < 1:
        raise ValueError("max_edge must be positive")
    with Image.open(io.BytesIO(data)) as opened:
        if int(getattr(opened, "n_frames", 1)) != 1:
            raise ValueError("animated images are not supported by region location")
        pixel_limit = Image.MAX_IMAGE_PIXELS
        if pixel_limit is not None and opened.width * opened.height > pixel_limit:
            raise ValueError("image exceeds Pillow's safe decoded-pixel budget")
        has_transparency = "transparency" in opened.info
        opened.load()
        display = ImageOps.exif_transpose(opened)
        display_width, display_height = display.size
        if "A" in display.getbands() or has_transparency:
            inference = display.convert("RGBA")
        else:
            inference = display.convert("RGB")
        inference.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        inference.save(encoded, format="PNG", optimize=False, compress_level=6)
    payload = encoded.getvalue()
    return ImageAsset(
        data=payload,
        media_type="image/png",
        width=inference.width,
        height=inference.height,
        sha256="sha256:" + hashlib.sha256(payload).hexdigest(),
        display_width=display_width,
        display_height=display_height,
    )


# These prompt profiles are qualified by current model documentation where it
# exists and by bounded OpenRouter probes on a non-square image.  A profile is
# deliberately exact/fail-closed; an unknown model is not assumed to inherit a
# coordinate convention merely because its output looks numerically plausible.
_HOSTED_VLM_PROFILES: tuple[RegionLocatorProfile, ...] = (
    RegionLocatorProfile(
        profile_id="claude-sonnet-5-xyxy-pixel-v1",
        model_patterns=("anthropic/claude-sonnet-5",),
        bbox_field="bbox_pixels_xyxy",
        coordinate_order="xyxy",
        coordinate_space="pixel",
        prompt_version="region-locate-claude-sonnet5-v1",
        reasoning_policy=None,
    ),
    RegionLocatorProfile(
        profile_id="openai-gpt5-xyxy-pixel-v1",
        model_patterns=(
            "openai/gpt-5",
            "openai/gpt-5-mini",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-terra",
            "openai/gpt-5.6-sol",
        ),
        bbox_field="bbox_pixels_xyxy",
        coordinate_order="xyxy",
        coordinate_space="pixel",
        prompt_version="region-locate-openai-gpt5-v1",
        reasoning_policy=None,
    ),
    RegionLocatorProfile(
        profile_id="gemini-3.6-flash-yxyx-1000-v1",
        model_patterns=("gemini/gemini-3.6-flash",),
        bbox_field="box_2d",
        coordinate_order="yxyx",
        coordinate_space="normalized_1000",
        prompt_version="region-locate-gemini36-v1",
        reasoning_policy=None,
    ),
    RegionLocatorProfile(
        profile_id="glm-5.3-flash-yxyx-1000-v1",
        model_patterns=("openrouter/z-ai/glm-5.3-flash",),
        bbox_field="box_2d",
        coordinate_order="yxyx",
        coordinate_space="normalized_1000",
        prompt_version="region-locate-glm53-v1",
        # Some OpenRouter routes reject disabled reasoning for this model.
        reasoning_policy="low_exclude",
    ),
    RegionLocatorProfile(
        profile_id="kimi-k3-xyxy-normalized-v1",
        model_patterns=("openrouter/moonshotai/kimi-k3",),
        bbox_field="bbox_normalized_xyxy",
        coordinate_order="xyxy",
        coordinate_space="normalized",
        prompt_version="region-locate-kimi-k3-v1",
        reasoning_policy="disabled",
    ),
    RegionLocatorProfile(
        profile_id="mimo-v2.5-yxyx-normalized-v1",
        model_patterns=("openrouter/xiaomi/mimo-v2.5",),
        bbox_field="bbox_normalized_yxyx",
        coordinate_order="yxyx",
        coordinate_space="normalized",
        prompt_version="region-locate-mimo-v25-v1",
        reasoning_policy="disabled",
    ),
    RegionLocatorProfile(
        profile_id="qwen3-vl-xyxy-1000-v1",
        model_patterns=(
            "openrouter/qwen/qwen3-vl-8b-instruct",
            "openrouter/qwen/qwen3-vl-32b-instruct",
        ),
        bbox_field="bbox_2d",
        coordinate_order="xyxy",
        coordinate_space="normalized_1000",
        prompt_version="region-locate-qwen3vl-v1",
        reasoning_policy="disabled",
    ),
)


def profile_for_engine(engine_ref: str) -> RegionLocatorProfile | None:
    """Return the explicit tested profile for ``engine_ref``, or fail closed."""

    normalized = engine_ref.strip().lower()
    for profile in _HOSTED_VLM_PROFILES:
        if normalized in {pattern.lower() for pattern in profile.model_patterns}:
            return profile
    return None


def _coordinate_instruction(profile: RegionLocatorProfile) -> str:
    order = (
        "[x_min, y_min, x_max, y_max]"
        if profile.coordinate_order == "xyxy"
        else "[y_min, x_min, y_max, x_max]"
    )
    if profile.coordinate_space == "normalized":
        space = "numbers from 0 to 1 normalized to image width and height"
    elif profile.coordinate_space == "normalized_1000":
        space = "integers from 0 to 1000 normalized to image width (x) and height (y)"
    else:
        space = "pixel coordinates in the supplied image"
    return f'Put each box in "{profile.bbox_field}" as {order}, using {space}.'


def _details_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if schema is None:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
            "required": [],
        }
    return schema


def region_response_schema(
    profile: RegionLocatorProfile,
    detail_schema: dict[str, Any] | None,
) -> dict[str, Any]:
    """Structured wire schema for one profile's native bbox field."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["matches"],
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["match", profile.bbox_field, "details"],
                    "properties": {
                        "match": {"type": "string", "minLength": 1},
                        profile.bbox_field: {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                        },
                        "details": _details_schema(detail_schema),
                    },
                },
            }
        },
    }


def _region_prompt(profile: RegionLocatorProfile, request: LocateRegionsRequest) -> str:
    return (
        "Find every distinct visible occurrence that satisfies this instruction: "
        f"{request.query}\n\n"
        "Return one match per distinct region, not an answer or summary. "
        "Do not group or merge separate occurrences. Include a partially visible "
        "occurrence when enough is visible to localize it. "
        "Use a tight axis-aligned box around visible pixels only; do not infer "
        "hidden or off-image extent. The origin is the top-left, x increases "
        "right, and y increases down. "
        "Do not include objects that do not satisfy the instruction. "
        "Use a short identifying label in match. "
        f"The supplied image is {request.image.width} by "
        f"{request.image.height} pixels. "
        f"{_coordinate_instruction(profile)} "
        "Fill details from the response schema for the same region in this one "
        "response. Return an empty matches list when there are none."
    )


def _region_messages(
    profile: RegionLocatorProfile,
    request: LocateRegionsRequest,
) -> list[dict[str, Any]]:
    encoded = base64.b64encode(request.image.data).decode("ascii")
    # Image first and no system role works across the qualified hosted models,
    # including GLM's OpenRouter contract.
    return [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "media_type": request.image.media_type,
                    "data": encoded,
                },
                {"type": "text", "text": _region_prompt(profile, request)},
            ],
        }
    ]


def region_request_identity(request: LocateRegionsRequest) -> dict[str, Any]:
    """Stable, complete request identity without embedding image base64."""

    profile = profile_for_engine(request.engine_ref)
    if profile is None:
        raise UnsupportedRegionLocator(
            f"no tested region-location profile for {request.engine_ref!r}"
        )
    return {
        "engine_ref": request.engine_ref,
        "profile": {
            "profile_id": profile.profile_id,
            "bbox_field": profile.bbox_field,
            "coordinate_order": profile.coordinate_order,
            "coordinate_space": profile.coordinate_space,
            "prompt_version": profile.prompt_version,
            "reasoning_policy": profile.reasoning_policy,
            "supports_detail_schema": profile.supports_detail_schema,
        },
        "prompt": _region_prompt(profile, request),
        "response_schema": region_response_schema(profile, request.detail_schema),
        "image": {
            "sha256": request.image.sha256,
            "media_type": request.image.media_type,
            "width": request.image.width,
            "height": request.image.height,
        },
    }


def _raw_xyxy(
    profile: RegionLocatorProfile,
    raw_bbox: Any,
    *,
    image_width: int,
    image_height: int,
) -> tuple[tuple[float, float, float, float], dict[str, Any]] | None:
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        return None
    # Strings are rejected rather than silently coerced into geometry.
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in raw_bbox
    ):
        return None
    values = tuple(float(value) for value in raw_bbox)
    if not all(math.isfinite(value) for value in values):
        return None
    if profile.coordinate_order == "xyxy":
        x0, y0, x1, y1 = values
    else:
        y0, x0, y1, x1 = values

    if profile.coordinate_space == "normalized":
        bounds = (1.0, 1.0)
    elif profile.coordinate_space == "normalized_1000":
        bounds = (1000.0, 1000.0)
    else:
        bounds = (float(image_width), float(image_height))
    if not (0 <= x0 < x1 <= bounds[0] and 0 <= y0 < y1 <= bounds[1]):
        return None
    return values, {
        "space": profile.coordinate_space,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
    }


def decode_region_response(
    profile: RegionLocatorProfile,
    data: Any,
    *,
    image_width: int,
    image_height: int,
    engine_ref: str,
) -> LocateRegionsResult:
    """Strictly decode one model response into canonical regions.

    Schema-valid items with bad geometry are reported rather than repaired.
    Good siblings remain usable, allowing the consumer to report partial
    coverage honestly. Whole-response shape failures remain the structured
    completer's existing repair/failure responsibility.
    """

    raw_matches = data.get("matches") if isinstance(data, dict) else None
    if not isinstance(raw_matches, list):
        return LocateRegionsResult(
            regions=(),
            issues=(
                RegionIssue(
                    code="invalid_response",
                    message="region response must contain a matches list",
                ),
            ),
        )

    provenance = RegionProvenance(
        engine_ref=engine_ref,
        profile_id=profile.profile_id,
        prompt_version=profile.prompt_version,
    )
    regions: list[LocatedRegion] = []
    issues: list[RegionIssue] = []
    seen: set[tuple[float, float, float, float]] = set()
    for index, item in enumerate(raw_matches):
        if not isinstance(item, dict):
            issues.append(
                RegionIssue(
                    code="invalid_match",
                    message="region match must be an object",
                    item_index=index,
                )
            )
            continue
        match = item.get("match")
        details = item.get("details")
        if (
            not isinstance(match, str)
            or not match.strip()
            or not isinstance(details, dict)
        ):
            issues.append(
                RegionIssue(
                    code="invalid_match",
                    message="region match requires a label and details object",
                    item_index=index,
                )
            )
            continue
        converted = _raw_xyxy(
            profile,
            item.get(profile.bbox_field),
            image_width=image_width,
            image_height=image_height,
        )
        if converted is None:
            issues.append(
                RegionIssue(
                    code="invalid_bbox",
                    message="region bbox is malformed, out of range, or degenerate",
                    item_index=index,
                )
            )
            continue
        raw_values, box = converted
        canonical = normalize_bbox(
            box,
            frame="page",
            width=image_width,
            height=image_height,
        )
        if canonical is None:
            issues.append(
                RegionIssue(
                    code="invalid_bbox",
                    message="region bbox could not be normalized",
                    item_index=index,
                )
            )
            continue
        identity = (
            float(canonical["x0"]),
            float(canonical["y0"]),
            float(canonical["x1"]),
            float(canonical["y1"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        regions.append(
            LocatedRegion(
                match=match.strip(),
                bbox=canonical,
                details=dict(details),
                raw_bbox=raw_values,
                provenance=provenance,
            )
        )
    regions.sort(
        key=lambda region: (
            float(region.bbox["y0"]),
            float(region.bbox["x0"]),
            float(region.bbox["y1"]),
            float(region.bbox["x1"]),
        )
    )
    return LocateRegionsResult(regions=tuple(regions), issues=tuple(issues))


class HostedVLMRegionLocator:
    """Region locator backed by Frisket's existing structured model router."""

    supports_detail_schema = True

    def __init__(self, router: Any) -> None:
        self.router = router

    async def locate(self, request: LocateRegionsRequest) -> LocateRegionsResult:
        profile = profile_for_engine(request.engine_ref)
        if profile is None:
            raise UnsupportedRegionLocator(
                f"no tested region-location profile for {request.engine_ref!r}"
            )
        if request.detail_schema is not None and not profile.supports_detail_schema:
            raise UnsupportedRegionLocator(
                f"{request.engine_ref!r} cannot return region details in one pass"
            )
        response = await StructuredCompleter(self.router).complete(
            StructuredRequest(
                model=request.engine_ref,
                messages=_region_messages(profile, request),
                schema=region_response_schema(profile, request.detail_schema),
                repair_attempts=1,
                max_tokens=32_768,
                temperature=0.0,
                reasoning_policy=profile.reasoning_policy,
            )
        )
        decoded = decode_region_response(
            profile,
            response.data,
            image_width=request.image.width,
            image_height=request.image.height,
            engine_ref=request.engine_ref,
        )
        return LocateRegionsResult(
            regions=decoded.regions,
            issues=decoded.issues,
            wire_calls=tuple(response.wire_calls),
        )
