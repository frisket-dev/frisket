from __future__ import annotations

from frisket.ops.subtitles import subtitle_text


def test_vtt_auto_caption_rolling_duplicates_collapse_to_deduped_transcript() -> None:
    # YouTube AUTO captions render as a rolling window: each cue repeats the
    # tail of the previous cue before appending new words.
    vtt = (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
        "Hello there\n"
        "\n"
        "00:00:02.000 --> 00:00:04.000 align:start position:0%\n"
        "Hello there\n"
        "this is a test\n"
        "\n"
        "00:00:04.000 --> 00:00:06.000 align:start position:0%\n"
        "this is a test\n"
        "rolling caption\n"
    ).encode("utf-8")

    assert (
        subtitle_text(vtt, "clip.en.vtt")
        == "Hello there\nthis is a test\nrolling caption"
    )


def test_srt_fixture_strips_indices_and_timestamps() -> None:
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:02,000\n"
        "Hello there\n"
        "\n"
        "2\n"
        "00:00:02,000 --> 00:00:04,000\n"
        "This is a test\n"
    ).encode("utf-8")

    assert subtitle_text(srt, "clip.srt") == "Hello there\nThis is a test"


def test_vtt_inline_timing_and_markup_tags_are_stripped() -> None:
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "<00:00:00.160><c> Hello</c> <00:00:00.500><c> there</c>\n"
        "\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "<b>Bold</b> and <i>italic</i> and <c.yellow>colored</c.yellow>\n"
        "\n"
        "00:00:04.000 --> 00:00:06.000\n"
        "Tom &amp; Jerry &lt;3&gt; say &nbsp;hi\n"
    ).encode("utf-8")

    assert subtitle_text(vtt, "clip.en.vtt") == (
        "Hello there\nBold and italic and colored\nTom & Jerry <3> say hi"
    )


def test_legitimately_repeated_dialogue_is_preserved() -> None:
    # A global line-uniq would destroy this; consecutive-only dedupe must not.
    vtt = (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Hello there\n"
        "\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "Nice weather today\n"
        "\n"
        "00:00:04.000 --> 00:00:06.000\n"
        "Hello there\n"
    ).encode("utf-8")

    assert (
        subtitle_text(vtt, "clip.en.vtt")
        == "Hello there\nNice weather today\nHello there"
    )


def test_unknown_caption_format_returns_none() -> None:
    assert subtitle_text(b'{"events": []}', "clip.json3") is None
    assert subtitle_text(b"some ttml body", "clip.ttml") is None
    assert subtitle_text(b"[Script Info]\n", "clip.ass") is None
    assert subtitle_text(b"<srv1/>", "clip.srv1") is None
    assert subtitle_text(b"<srv3/>", "clip.srv3") is None


def test_no_extractable_text_returns_none() -> None:
    vtt = b"WEBVTT\nKind: captions\nLanguage: en\n"
    assert subtitle_text(vtt, "clip.en.vtt") is None
