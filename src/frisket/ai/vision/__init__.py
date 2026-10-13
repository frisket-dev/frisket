"""Reusable visual-analysis interfaces.

The modules in this package are action-neutral: they know how to turn an
image plus an instruction into canonical observations, but know nothing about
sheets, rows, evidence publication, or any one action's coverage policy.
"""

from .coordinates import (
    CLAUDE_HIGH_RES_COORDINATE_PROFILE,
    CLAUDE_STANDARD_COORDINATE_PROFILE,
    CoordinateOrder,
    CoordinateProfile,
    CoordinateSpace,
    DecodedBoundingBox,
    GEMINI_COORDINATE_PROFILE,
    GENERIC_COORDINATE_PROFILE,
    ImageDetail,
    OPENAI_GPT56_COORDINATE_PROFILE,
    OPENAI_GPT5_COORDINATE_PROFILE,
    OPENAI_GPT6_COORDINATE_PROFILE,
    coordinate_profile_for_model,
    coordinate_instruction,
    decode_bounding_box,
    inference_image_size,
)
from .region_locator import (
    HostedVLMRegionLocator,
    ImageAsset,
    LocatedRegion,
    LocateRegionsRequest,
    LocateRegionsResult,
    RegionIssue,
    RegionLocator,
    RegionLocatorProfile,
    RegionProvenance,
    UnsupportedRegionLocator,
    decode_region_response,
    prepare_image_asset,
    profile_for_engine,
)

__all__ = [
    "CLAUDE_HIGH_RES_COORDINATE_PROFILE",
    "CLAUDE_STANDARD_COORDINATE_PROFILE",
    "CoordinateOrder",
    "CoordinateProfile",
    "CoordinateSpace",
    "DecodedBoundingBox",
    "GEMINI_COORDINATE_PROFILE",
    "GENERIC_COORDINATE_PROFILE",
    "HostedVLMRegionLocator",
    "ImageAsset",
    "ImageDetail",
    "LocatedRegion",
    "LocateRegionsRequest",
    "LocateRegionsResult",
    "OPENAI_GPT5_COORDINATE_PROFILE",
    "OPENAI_GPT56_COORDINATE_PROFILE",
    "OPENAI_GPT6_COORDINATE_PROFILE",
    "RegionIssue",
    "RegionLocator",
    "RegionLocatorProfile",
    "RegionProvenance",
    "UnsupportedRegionLocator",
    "coordinate_instruction",
    "coordinate_profile_for_model",
    "decode_bounding_box",
    "decode_region_response",
    "inference_image_size",
    "prepare_image_asset",
    "profile_for_engine",
]
