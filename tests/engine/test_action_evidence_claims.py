import pytest
from pydantic import ValidationError

from frisket.actions.grounding_types import EvidenceBox, EvidenceClaim


def test_claim_roundtrip_preserves_list_index_and_existing_coordinate_spaces():
    claim = EvidenceClaim(
        item_index=1,
        segment_indices=(2, 3),
        page=0,
        bbox=EvidenceBox(x0=20, y0=30, x1=50, y1=70, space="page_1000"),
        grounding_method="quote",
    )
    assert EvidenceClaim.model_validate_json(claim.model_dump_json()) == claim
    assert claim.bbox.space == "page_1000"


@pytest.mark.parametrize("field", ["source_id", "blob_hash", "span_id", "metadata"])
def test_claim_cannot_supply_stored_authority(field):
    with pytest.raises(ValidationError):
        EvidenceClaim.model_validate({"quote": "hello", field: 42})


def test_claim_and_box_are_immutable():
    claim = EvidenceClaim(quote="hello", bbox=EvidenceBox(x0=0, y0=0, x1=1, y1=1))
    with pytest.raises(ValidationError):
        claim.quote = "changed"
    with pytest.raises(ValidationError):
        claim.bbox.x0 = 0.5
