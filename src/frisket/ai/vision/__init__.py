"""Reusable visual-analysis interfaces.

The modules in this package are action-neutral: they know how to turn an
image plus an instruction into canonical observations, but know nothing about
sheets, rows, evidence publication, or any one action's coverage policy.
"""

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
    "HostedVLMRegionLocator",
    "ImageAsset",
    "LocatedRegion",
    "LocateRegionsRequest",
    "LocateRegionsResult",
    "RegionIssue",
    "RegionLocator",
    "RegionLocatorProfile",
    "RegionProvenance",
    "UnsupportedRegionLocator",
    "decode_region_response",
    "prepare_image_asset",
    "profile_for_engine",
]
