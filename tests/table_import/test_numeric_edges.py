from frisket.table_import import RawCell, convert_cell


def test_currency_and_whitespace_at_number_edges_preserve_conversion() -> None:
    for original in ("$ 123 €", "\u2003£\u2003123\u2003¥\u2003", "123", "123.0"):
        result = convert_cell(RawCell(original), "integer")
        assert result.value == 123
        assert result.outcome == "unchanged"
    assert convert_cell(RawCell("(£ 123 ¥)"), "integer").value == -123
    assert convert_cell(RawCell("$ 12.5 %"), "number").value == 0.125
    for original in ("12 $ 3", "$ £ €", "54.863"):
        assert convert_cell(RawCell(original), "integer").outcome == "invalid"


def test_long_internal_currency_or_space_run_does_not_become_a_number() -> None:
    for character in (" ", "$", "\u2003"):
        original = "123" + character * (2 * 1024 * 1024) + "invalid"
        result = convert_cell(RawCell(original), "number")
        assert result.outcome == "invalid"
        assert result.original == original
