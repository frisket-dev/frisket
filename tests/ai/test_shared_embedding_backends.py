from __future__ import annotations

from frisket.ai.embeddings import backends


def test_backend_resolution_requires_explicit_remote_opt_in(monkeypatch):
    monkeypatch.setattr(backends, "local_embedder", lambda: None)

    class Router:
        def has_embedding_backend(self):
            return True

    assert backends.resolve_embedder(Router(), allow_remote=False) is None
    resolved = backends.resolve_embedder(Router(), allow_remote=True)
    assert resolved is not None
    assert resolved[1] == backends.REMOTE_EMBED_MODEL


def test_local_backends_honor_disable_switch_without_loading_models():
    disabled = {"FRISKET_DISABLE_LOCAL_EMBED": "1"}
    assert backends.local_embedder(env=disabled) is None
    assert backends.local_image_embedder(env=disabled) is None
    assert backends.local_reranker(env=disabled) is None
