"""Current-epoch corpus for execution route records.

GD-07 reset the named promise fixtures to the six-key row schema. Future
unknown additions remain decode-preserved, while retired shapes and stale
hashes refuse rather than gaining a compatibility decoder.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.execution.promises import (
    REGISTRY,
    SATISFIED,
    UNEVALUABLE,
    Promise,
    PromiseSet,
    evaluate,
    evaluate_set,
)

CORPUS = Path(__file__).parent / "route_corpus"

GEN1_FIXTURES = sorted(CORPUS.glob("promise_set_gen1_*.json"))
ALL_PROMISE_SET_FIXTURES = sorted(CORPUS.glob("promise_set_*.json"))


def test_corpus_is_present():
    assert {path.name for path in GEN1_FIXTURES} == {
        "promise_set_gen1_local_free.json",
        "promise_set_gen1_remote_unbounded.json",
    }
    assert (CORPUS / "promise_set_future_unknowns.json").exists()
    assert (CORPUS / "registry_tables_gen1.json").exists()


@pytest.mark.parametrize("path", ALL_PROMISE_SET_FIXTURES, ids=lambda p: p.stem)
def test_promise_set_fixture_round_trips(path: Path):
    """Reader round-trip: decode -> encode -> decode is lossless, including
    material this reader does not understand (decode-preserve)."""
    blob = path.read_text()
    ps = PromiseSet.from_json(blob)
    again = PromiseSet.from_json(ps.to_json())
    assert again == ps
    # No key of the original document (row-level or document-level) was
    # dropped or mangled by the trip through the typed reader.
    original = json.loads(blob)
    encoded = json.loads(ps.to_json())
    assert encoded == {**original, "promises": encoded["promises"]}
    for original_row, encoded_row in zip(
        original["promises"], encoded["promises"], strict=True
    ):
        for key, value in original_row.items():
            assert encoded_row[key] == value


@pytest.mark.parametrize("path", GEN1_FIXTURES, ids=lambda p: p.stem)
def test_gen1_pinned_hashes_never_drift(path: Path):
    """The canonical hash contract's regression anchor: recomputing the
    content hash of a gen-1 recorded set must reproduce the pinned value
    forever (consent rows bind to this identity)."""
    doc = json.loads(path.read_text())
    ps = PromiseSet.from_json(path.read_text())
    assert ps.set_hash == doc["promise_set_hash"]


def test_gen1_sets_evaluate_at_their_recorded_facts():
    bindings = {
        "promise_set_gen1_local_free": {
            "operator": "self",
            "egress_class": "none",
            "credential_source": "local",
        },
        "promise_set_gen1_remote_unbounded": {
            "operator": "openai",
            "egress_class": "third_party_api",
            "credential_source": "org_byok",
        },
    }
    for path in GEN1_FIXTURES:
        ps = PromiseSet.from_json(path.read_text())
        ev = evaluate_set(ps.promises, bindings[path.stem])
        assert ev.overall == SATISFIED, (path.stem, ev)


def test_future_unknowns_decode_preserve_and_fail_closed():
    path = CORPUS / "promise_set_future_unknowns.json"
    ps = PromiseSet.from_json(path.read_text())
    glob_row, retention_row = ps.promises

    # Unknown op: preserved verbatim and recorded as unevaluable.
    assert glob_row.op == "matches_glob"
    assert json.loads(glob_row.extras_json)["future_column"] == 7
    result = evaluate(glob_row, {"region": "eu-west"})
    assert result.status == UNEVALUABLE and result.reason == "unknown_op"

    # Unknown field with a known op: preserved; no resolver produces the
    # fact, so it is unevaluable (missing_fact), never a crash or a pass.
    assert retention_row.field == "data_retention_days"
    result2 = evaluate(retention_row, {"operator": "self"})
    assert result2.status == UNEVALUABLE and result2.reason == "missing_fact"

    ev = evaluate_set(ps.promises, {"region": "eu-west"})
    assert ev.overall == UNEVALUABLE

    # Unknown document-level keys survive too.
    doc = json.loads(ps.to_json())
    assert doc["future_document_flag"] == {"introduced_by": "a-2028-writer"}
    assert doc["schema_gen"] == 2


def test_registry_tables_match_their_pins():
    """A (kind, ref) once referenced by any recorded promise resolves
    forever, identically. The fixture is the pin; the live registry must
    match it exactly."""
    pins = json.loads((CORPUS / "registry_tables_gen1.json").read_text())
    for ref, expected in pins["order"].items():
        table = REGISTRY.order_table(ref)
        assert table is not None, f"pinned order table {ref} vanished"
        assert list(table.chain) == expected["chain"]
        assert list(table.unordered) == expected["unordered"]
    for ref, expected in pins["throughput"].items():
        table = REGISTRY.throughput_table(ref)
        assert table is not None, f"pinned throughput table {ref} vanished"
        assert [list(pair) for pair in table.factors] == expected["factors"]


def test_hash_family_pins_never_drift():
    """A2: EVERY hash family is corpus-pinned — declared rules + a sample
    document + its digest. The one implementation (content_hash) must
    reproduce each pin forever; a drift means recorded identity broke."""
    import unicodedata

    from frisket.execution.promises import HASH_FAMILIES, content_hash

    pins = json.loads((CORPUS / "hash_families_gen1.json").read_text())
    assert set(pins["families"]) == set(HASH_FAMILIES), (
        "every declared hash family must be pinned (and vice versa); a new "
        "family needs a new fixture entry"
    )
    for domain, entry in pins["families"].items():
        family = HASH_FAMILIES[domain]
        assert entry["rules"] == {
            "nfc_strings": family.nfc_strings,
            "float_policy": family.float_policy,
            "output": family.output,
        }, f"declared rules drifted for {domain}"
        assert content_hash(domain, entry["sample"]) == entry["pinned_hash"], domain

    # NFC is load-bearing for action identity (F4): the fixture's sample is
    # stored in DECOMPOSED (NFD) spelling; its composed (NFC) spelling must
    # hash to the SAME pinned digest — NFD/NFC-equivalent params can never
    # split a consent identity.
    identity = pins["families"]["frisket.action_identity.v1"]

    def _nfc(obj):
        if isinstance(obj, str):
            return unicodedata.normalize("NFC", obj)
        if isinstance(obj, list):
            return [_nfc(v) for v in obj]
        if isinstance(obj, dict):
            return {_nfc(k): _nfc(v) for k, v in obj.items()}
        return obj

    nfc_sample = _nfc(identity["sample"])
    assert nfc_sample != identity["sample"]  # the stored sample really is NFD
    assert (
        content_hash("frisket.action_identity.v1", nfc_sample)
        == identity["pinned_hash"]
    )


def test_promise_set_identity_is_singular():
    """``PromiseSet.set_hash``, the store's row-level hash, and the
    consented_set_hash are ONE construction — equal digests for the same
    semantic set, from cold fixtures."""
    from frisket.engine.store.execution_routes import (
        promise_set_hash as store_promise_set_hash,
    )
    from frisket.execution.resolve_for_action import consented_set_hash

    for path in GEN1_FIXTURES:
        doc = json.loads(path.read_text())
        ps = PromiseSet.from_json(path.read_text())
        assert ps.set_hash == doc["promise_set_hash"]
        assert store_promise_set_hash(doc["promises"]) == doc["promise_set_hash"]
        assert consented_set_hash(ps) == doc["promise_set_hash"]


def test_promise_fingerprint_domain_is_singular():
    """F9: one fingerprint construction (frisket.promise_row.v1) shared by
    the execution helper and the store's violation writer."""
    from frisket.engine.store.execution_routes import (
        promise_fingerprint as store_promise_fingerprint,
    )
    from frisket.execution.promises import promise_row_fingerprint

    for path in GEN1_FIXTURES:
        doc = json.loads(path.read_text())
        ps = PromiseSet.from_json(path.read_text())
        for promise, row in zip(ps.promises, doc["promises"], strict=True):
            assert promise_row_fingerprint(promise.to_row()) == (
                store_promise_fingerprint(row)
            )


def test_gen1_rows_redecode_identically_via_from_row():
    """Row-level reader: Promise.from_row over raw fixture rows equals the
    document reader's output (no second decode path drifting)."""
    for path in GEN1_FIXTURES:
        doc = json.loads(path.read_text())
        ps = PromiseSet.from_json(path.read_text())
        rebuilt = tuple(Promise.from_row(row) for row in doc["promises"])
        assert rebuilt == ps.promises
