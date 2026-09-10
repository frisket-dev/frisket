from __future__ import annotations

import json

import pytest

from parser import LayoutOutputError, parse_page, smart_resize


def test_factor_28_scaling_maps_boxes_back_to_original_pixels() -> None:
    # The vision input rounds 1000x500 to 1008x504. Model-space bboxes must be
    # mapped back before Frisket normalizes them against the original render.
    assert smart_resize(500, 1000) == (504, 1008)
    output = json.dumps(
        [
            {
                "bbox": [100.8, 50.4, 504, 252],
                "category": "Text",
                "text": "first",
            },
            {
                "bbox": [504, 252, 1008, 504],
                "category": "Table",
                "text": "<table><tr><td>second</td></tr></table>",
            },
        ]
    )

    page = parse_page(output, width=1000, height=500)

    table = "<table><tr><td>second</td></tr></table>"
    assert page["text"] == f"first\n\n{table}"
    assert page["blocks"] == [
        {
            "text": "first",
            "bbox": [[100, 50], [500, 50], [500, 250], [100, 250]],
        },
        {
            "text": table,
            "bbox": [[500, 250], [1000, 250], [1000, 500], [500, 500]],
        },
    ]


def test_table_html_is_preserved() -> None:
    output = json.dumps(
        [
            {
                "bbox": [0, 0, 280, 280],
                "category": "Table",
                "text": (
                    "<table><tr><th>Name</th><th>Value</th></tr>"
                    "<tr><td>A &amp; B</td><td>42</td></tr></table>"
                ),
            }
        ]
    )

    page = parse_page(output, width=280, height=280)

    table = (
        "<table><tr><th>Name</th><th>Value</th></tr>"
        "<tr><td>A &amp; B</td><td>42</td></tr></table>"
    )
    assert page["text"] == table
    assert page["blocks"][0]["text"] == table


def test_picture_without_text_is_preserved_and_code_fence_is_accepted() -> None:
    output = """```json
[{"bbox":[0,0,56,56],"category":"Picture"}]
```"""

    assert parse_page(output, width=28, height=28) == {
        "text": "",
        "blocks": [{"text": "", "bbox": [[0, 0], [28, 0], [28, 28], [0, 28]]}],
    }


def test_other_category_is_accepted() -> None:
    output = '[{"bbox":[0,0,28,28],"category":"Other","text":"stamp"}]'

    assert parse_page(output, width=28, height=28)["text"] == "stamp"


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "{}",
        '[{"bbox":[0,0,10],"category":"Text","text":"x"}]',
        '[{"bbox":[10,0,0,10],"category":"Text","text":"x"}]',
        '[{"bbox":[0,0,10,10],"category":"Unknown","text":"x"}]',
        '[{"bbox":[0,0,10,10],"category":"Text"}]',
    ],
)
def test_malformed_layout_output_fails_closed(output: str) -> None:
    with pytest.raises(LayoutOutputError):
        parse_page(output, width=100, height=100)
