import pytest

from frisket.pdf_packets.source_map import SourceMap, SourceOccurrence


def test_source_occurrences_resolve_pages_and_never_cross_boundaries():
    sources = SourceMap(
        (
            SourceOccurrence("row-a", "same-blob", 2, 4),
            SourceOccurrence("row-b", "same-blob", 1, 2),
        )
    )
    assert sources.page_count == 5
    assert sources.fixed_starts == (1, 4)
    assert (sources.resolve(1).occurrence.key, sources.resolve(1).local_page) == (
        "row-a",
        2,
    )
    assert (sources.resolve(4).occurrence.key, sources.resolve(4).local_page) == (
        "row-b",
        1,
    )
    ranges = sources.ranges({2, 5})
    assert [(r.occurrence.key, r.start, r.end) for r in ranges] == [
        ("row-a", 2, 2),
        ("row-a", 3, 4),
        ("row-b", 1, 1),
        ("row-b", 2, 2),
    ]
    assert [(r.occurrence.key, r.start, r.end) for r in sources.page_ranges()] == [
        ("row-a", 2, 2),
        ("row-a", 3, 3),
        ("row-a", 4, 4),
        ("row-b", 1, 1),
        ("row-b", 2, 2),
    ]


def test_source_map_rejects_invalid_identity_extent_and_page():
    with pytest.raises(ValueError):
        SourceMap(
            (SourceOccurrence("same", "a", 1, 1), SourceOccurrence("same", "b", 1, 1))
        )
    with pytest.raises(ValueError):
        SourceOccurrence("a", "blob", 3, 2)
    sources = SourceMap((SourceOccurrence("a", "blob", 1, 2),))
    with pytest.raises(ValueError):
        sources.resolve(3)
    with pytest.raises(ValueError):
        sources.ranges({0})
