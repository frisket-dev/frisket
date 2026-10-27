"""Ordered source occurrences and their original-local page ranges."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceOccurrence:
    key: str
    artifact_id: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.key, str)
            or not self.key
            or not isinstance(self.artifact_id, str)
            or not self.artifact_id
        ):
            raise ValueError("source key and artifact identity are required")
        if (
            type(self.start) is not int
            or type(self.end) is not int
            or not 1 <= self.start <= self.end
        ):
            raise ValueError("source extent must be an inclusive positive page range")


@dataclass(frozen=True)
class PageAddress:
    occurrence: SourceOccurrence
    local_page: int


@dataclass(frozen=True)
class SourceRange:
    occurrence: SourceOccurrence
    start: int
    end: int


class SourceMap:
    def __init__(self, occurrences: Sequence[SourceOccurrence]) -> None:
        self.occurrences = tuple(occurrences)
        if not self.occurrences or len(
            {source.key for source in self.occurrences}
        ) != len(self.occurrences):
            raise ValueError(
                "source occurrences must have distinct keys and cannot be empty"
            )
        offsets = []
        count = 0
        for source in self.occurrences:
            offsets.append(count + 1)
            count += source.end - source.start + 1
        self.fixed_starts = tuple(offsets)
        self.page_count = count

    def resolve(self, page: int) -> PageAddress:
        if type(page) is not int or not 1 <= page <= self.page_count:
            raise ValueError("page outside source map")
        for source, offset in reversed(
            tuple(zip(self.occurrences, self.fixed_starts, strict=True))
        ):
            if page >= offset:
                return PageAddress(source, source.start + page - offset)
        raise AssertionError("unreachable")

    def ranges(self, starts: Collection[int]) -> tuple[SourceRange, ...]:
        for page in starts:
            if type(page) is not int or not 1 <= page <= self.page_count:
                raise ValueError("start outside source map")
        boundaries = sorted(set(starts) | set(self.fixed_starts))
        result = []
        for index, page in enumerate(boundaries):
            address = self.resolve(page)
            next_page = (
                boundaries[index + 1]
                if index + 1 < len(boundaries)
                else self.page_count + 1
            )
            end = address.local_page + next_page - page - 1
            result.append(SourceRange(address.occurrence, address.local_page, end))
        return tuple(result)

    def page_ranges(self) -> tuple[SourceRange, ...]:
        return self.ranges(range(1, self.page_count + 1))
