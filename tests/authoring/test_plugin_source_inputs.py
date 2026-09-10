from frisket.contracts.plugin import PluginManifestRuntimeBinding


def test_projection_input_vocabulary_remains_available() -> None:
    binding = PluginManifestRuntimeBinding.model_validate(
        {
            "kind": "demo.projection.timeline",
            "handler_api": "plugin_projection",
            "inputs": [{"name": "date", "types": ["date"], "optional": True}],
        }
    )

    assert binding.inputs == [{"name": "date", "types": ["date"], "optional": True}]
