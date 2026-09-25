from types import SimpleNamespace


def test_local_factory_exposes_only_cpu_docling(monkeypatch):
    from frisket_models import local

    captured = {}

    def create_sidecar_app(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("FRISKET_LOCAL_MODELS_TOKEN", "internal-token")
    monkeypatch.setattr(local, "create_sidecar_app", create_sidecar_app)
    local.create_app()

    engine = captured["registry"].get("docling")
    assert captured["token"] == "internal-token"
    assert captured["concurrency"] == 1
    assert engine.route == "/to-markdown"
    assert engine.loader.keywords == {"device": "cpu"}
