import pytest

from frisket.ops.markdown_plain import markdown_to_plain_text


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("# Heading", "Heading"),
        ("**Bold** and *italic*", "Bold and italic"),
        ("[label](https://example.com/a_(b))", "label"),
        ("Use `**literal asterisks**`", "Use **literal asterisks**"),
        ("```html\n<div>hello</div>\n```", "<div>hello</div>"),
        (r"\*literal\* &amp; text", "*literal* & text"),
        ("![A diagram](https://example.com/image.png)", "A diagram"),
        ("<p>One<br>Two</p>", "One\nTwo"),
        ("Plaintiff <John Doe> v. State", "Plaintiff <John Doe> v. State"),
        ("Plaintiff <Mark Doe> v. State", "Plaintiff <Mark Doe> v. State"),
        ("Filing <party/>", "Filing <party/>"),
        ("<TABLE><TR><TD>Filed</TD></TR></TABLE>", "Filed"),
        ('First<BR>Second <IMG ALT="diagram">', "First\nSecond diagram"),
        (
            "Plaintiff <Mark Doe> and <mark>flagged</mark>",
            "Plaintiff <Mark Doe> and flagged",
        ),
        ("1. First\n2. Second", "1. First\n2. Second"),
        ("3. Third\n4. Fourth", "3. Third\n4. Fourth"),
        (
            "1. Plaintiff <John Doe>\n2. Defendant State",
            "1. Plaintiff <John Doe>\n2. Defendant State",
        ),
        ("- Total **12**\n  **34** units", "Total 12\n34 units"),
        ("1. **Plaintiff**\n   **Defendant**", "1. Plaintiff\nDefendant"),
        ("<Mark Doe>\n\nDefendant", "<Mark Doe>\n\nDefendant"),
        ("<Mark Doe>\nv. State of Ohio", "<Mark Doe>\nv. State of Ohio"),
    ],
)
def test_plain_text_preserves_content(markdown, expected):
    assert markdown_to_plain_text(markdown) == expected


def test_tables_preserve_labels_and_values_without_markup():
    for markdown in (
        "| Name | Amount |\n| --- | --- |\n| River Press | 120.00 |",
        "<table><tr><th>Name</th><th>Amount</th></tr><tr><td>River Press</td><td>120.00</td></tr></table>",
    ):
        text = markdown_to_plain_text(markdown)
        assert text.split() == ["Name", "Amount", "River", "Press", "120.00"]
        assert "|" not in text and "<" not in text


def test_tables_project_rows_and_cells_without_layout_whitespace():
    markdown = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    assert markdown_to_plain_text(markdown) == "A\tB\n1\t2"
