from __future__ import annotations

from PIL import Image, ImageDraw
import pytest

from frisket.pdf_packets import (
    PHASH_BITS,
    match_packet_pages,
    page_perceptual_signature,
    signature_similarity,
)


# Cached from the bounded 196-page FOIA experiment.  The first thirteen pages
# are the manually inspected memo-cover family; the final three are their
# closest non-family pages from the page-1 pHash neighborhood.  Keeping only
# signatures makes this a small deterministic fixture, not a bundled PDF.
FOIA_SIGNATURES = {
    1: 0xE38C4C49CC71F1369BB213961337363396EC6C49E4CD6EC996C83598D8379336,
    10: 0xE38C4CE9CC71B1369BB2339693B6363294696CC9E4CD6FC9C5C93C9A90369332,
    17: 0xB38C4C6DCC71F13F993233B693B61731B66D6CC9E44D6CC9C4C9370092369336,
    36: 0xF38C4C6DCC71F13E9B36339693361632B4E96C49E44D6CC985D9339C90769332,
    70: 0xE38CCCCD8C71D32E99B3139693373791B4E92CC9C44D66C9C7C91B6C91B29136,
    81: 0xE38CCC6D8C71D136D1B23B96B3B63E2294E96C4DC4CD66C94FC9919498B69332,
    91: 0xE38CCC4D8C63D3379E7733B613B63630952B2C49E4CD6CC8C2CB339899369B32,
    126: 0xE38CCC798C71732E1B923396933296F1C4CD6C4964C93DC8D5BE9A5293B299B2,
    131: 0xE38CCCED8C71D13E91B61B92B3973E32956D6C49C4C965C98EC9358892369BB2,
    136: 0xE38CCC4D8C71D337993233969336379394BD2CC9C4CD6C49E6C919B2919693B6,
    148: 0xE38CCC658C73F13699931393925B36C8C4ED6CCDC0EC6FC1D9B6993290B34B32,
    181: 0xE38CCC69CC71F3269B3233969336379B96282CC9E4C96CC9C5C93F9091B61B36,
    192: 0xE30CCC4D8CF1F3369BB213A693323649E4C96CCDC0CD2ED191B61B36DA339B92,
    88: 0xBE17966187E06506C3F293B5607F6C5EE06F608D903D4AD4C43C1BF0CC3F1F98,
    19: 0xBE071D83C6F0622787C092F867636E7895E83D1E607F97C0C0EE39C2952F681F,
    168: 0xBF00D12BC0FFB037D0BF4F24C0BF1ED8696B6C488FC819D0CAD62789D0B53BC1,
}
MEMO_FAMILY = {1, 10, 17, 36, 70, 81, 91, 126, 131, 136, 148, 181, 192}


def _template(*, lower_box: bool, dense_text: bool) -> Image.Image:
    image = Image.new("RGB", (480, 640), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((210, 35, 270, 95), outline="black", width=5)
    draw.rectangle((70, 120, 410, 150), outline="black", width=4)
    draw.rectangle((70, 175, 410, 205), outline="black", width=4)
    if lower_box:
        draw.rectangle((180, 270, 390, 430), outline="black", width=5)
    step = 15 if dense_text else 36
    for y in range(230, 590, step):
        draw.line((50, y, 160 if lower_box else 430, y), fill="black", width=3)
    return image


def test_signature_is_fixed_width_deterministic_and_allows_blank_pages():
    blank = Image.new("RGB", (480, 640), "white")
    first = page_perceptual_signature(blank)
    second = page_perceptual_signature(blank.resize((240, 320)))

    assert PHASH_BITS == 256
    assert type(first) is int and 0 <= first < 1 << PHASH_BITS
    assert first == second
    assert signature_similarity(first, second) == 1.0


def test_signature_keeps_coarse_layout_ahead_of_text_density():
    sparse_template = page_perceptual_signature(
        _template(lower_box=True, dense_text=False)
    )
    dense_template = page_perceptual_signature(
        _template(lower_box=True, dense_text=True)
    )
    different_layout = page_perceptual_signature(
        _template(lower_box=False, dense_text=True)
    )

    assert signature_similarity(sparse_template, dense_template) > signature_similarity(
        sparse_template, different_layout
    )


def test_cached_foia_memo_family_is_a_clean_native_hamming_neighborhood():
    for query in MEMO_FAMILY:
        ranked = sorted(
            (page for page in FOIA_SIGNATURES if page != query),
            key=lambda page: (
                -signature_similarity(FOIA_SIGNATURES[query], FOIA_SIGNATURES[page]),
                page,
            ),
        )
        assert set(ranked[:12]) == MEMO_FAMILY - {query}

    result = match_packet_pages(
        page_count=192,
        signatures=FOIA_SIGNATURES,
        confirmed={1, 10, 17, 36},
        threshold=70,
    )
    assert result.kinds[0].confirmed_pages == (1, 10, 17, 36)
    assert set(result.suggested_pages) == MEMO_FAMILY - {1, 10, 17, 36}


@pytest.mark.parametrize("value", [True, -1, 1 << 256])
def test_similarity_rejects_values_outside_the_signature_contract(value):
    with pytest.raises(ValueError, match="256-bit"):
        signature_similarity(0, value)


def test_signature_requires_a_real_pillow_image():
    with pytest.raises(TypeError, match="PIL Image"):
        page_perceptual_signature(b"not an image")  # type: ignore[arg-type]
