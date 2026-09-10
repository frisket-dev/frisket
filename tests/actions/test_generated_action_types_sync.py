from scripts.ci.gen_http_contracts import ACTION_TYPES_ARTIFACT, render_action_types


def test_generated_action_types_are_fresh() -> None:
    # rule19: compares a generated frontend cache with its Python authority
    assert ACTION_TYPES_ARTIFACT.read_text(encoding="utf-8") == render_action_types()
