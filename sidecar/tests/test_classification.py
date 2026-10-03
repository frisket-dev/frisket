from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import pytest

from frisket_models.classification import GLiClassBase, Jeff
from frisket_models.classification import gliclass_base, jeff


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path / "snapshots" / ("a" * 40)
    path.mkdir(parents=True)
    return path


class _GLiTokenizer:
    def __init__(self, token_count: int = 32):
        self.token_count = token_count

    def encode(self, _value: str):
        return list(range(self.token_count))


class _GLiPipeline:
    def __init__(self, *, winner: int = 1, score: float = 0.75):
        self.pipe = self
        self.winner = winner
        self.score = score
        self.calls = []

    def prepare_input(self, text, labels, *, prompt):
        return " ".join([*labels, prompt, text])

    def __call__(self, text, labels, **kwargs):
        self.calls.append((text, list(labels), kwargs))
        return [[{"label": labels[self.winner], "score": self.score}]]


class _JeffModel:
    def __init__(self, probabilities):
        self.probabilities = probabilities
        self.calls = []

    def predict(self, rows, *, batch_size):
        self.calls.append((rows, batch_size))
        return [self.probabilities]


def test_gliclass_loads_only_the_absolute_local_snapshot(monkeypatch, snapshot):
    calls = {}
    tokenizer = _GLiTokenizer()
    pipeline = _GLiPipeline()

    class FakeModel:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["model"] = (path, kwargs)
            return SimpleNamespace()

    class FakeAutoTokenizer:
        @classmethod
        def from_pretrained(cls, path, **kwargs):
            calls["tokenizer"] = (path, kwargs)
            return tokenizer

    def fake_pipeline(model, loaded_tokenizer, **kwargs):
        calls["pipeline"] = (model, loaded_tokenizer, kwargs)
        return pipeline

    monkeypatch.setitem(
        sys.modules,
        "gliclass",
        SimpleNamespace(
            GLiClassModel=FakeModel,
            ZeroShotClassificationPipeline=fake_pipeline,
        ),
    )
    monkeypatch.setitem(
        sys.modules, "transformers", SimpleNamespace(AutoTokenizer=FakeAutoTokenizer)
    )

    classifier = GLiClassBase(snapshot)

    assert calls["model"] == (
        str(snapshot),
        {"local_files_only": True, "trust_remote_code": False},
    )
    assert calls["tokenizer"] == (
        str(snapshot),
        {
            "add_prefix_space": True,
            "local_files_only": True,
            "trust_remote_code": False,
        },
    )
    assert calls["pipeline"][2] == {
        "classification_type": "single-label",
        "device": "cpu",
        "max_classes": 255,
        "max_length": 512,
        "progress_bar": False,
    }
    assert classifier.model_revision == snapshot.name


def test_gliclass_preserves_dotted_ids_duplicate_descriptions_and_order(
    monkeypatch, snapshot
):
    pipeline = _GLiPipeline(winner=1, score=0.625)
    monkeypatch.setattr(
        gliclass_base, "_load_runtime", lambda _path: (pipeline, _GLiTokenizer())
    )
    classifier = GLiClassBase(snapshot)
    labels = ["topic.alpha", "topic.beta"]

    result = classifier.classify(
        "A beta record",
        labels,
        descriptions={"topic.alpha": "same", "topic.beta": "same"},
        instruction="Choose a topic",
    )

    assert result == {
        "label": "topic.beta",
        "score": 0.625,
        "model_revision": snapshot.name,
    }
    _, model_labels, kwargs = pipeline.calls[0]
    assert len(model_labels) == 2
    assert model_labels[0] != model_labels[1]
    assert "topic.alpha" in model_labels[0]
    assert "topic.beta" in model_labels[1]
    assert kwargs["prompt"] == "Choose a topic"
    assert kwargs["return_hierarchical"] is False


def test_gliclass_rejects_combined_input_instead_of_truncating(monkeypatch, snapshot):
    pipeline = _GLiPipeline()
    monkeypatch.setattr(
        gliclass_base,
        "_load_runtime",
        lambda _path: (pipeline, _GLiTokenizer(token_count=513)),
    )

    with pytest.raises(ValueError, match="512-token limit.*no input was truncated"):
        GLiClassBase(snapshot).classify(
            "long text",
            ["one", "two"],
            descriptions={},
            instruction="choose",
        )

    assert pipeline.calls == []


def test_gliclass_accepts_more_than_25_labels(monkeypatch, snapshot):
    pipeline = _GLiPipeline(winner=25)
    monkeypatch.setattr(
        gliclass_base, "_load_runtime", lambda _path: (pipeline, _GLiTokenizer())
    )
    labels = [f"label-{index}" for index in range(26)]

    result = GLiClassBase(snapshot).classify(
        "text", labels, descriptions={}, instruction="choose"
    )

    assert result["label"] == "label-25"


def test_jeff_loader_is_cpu_local_only(monkeypatch, snapshot):
    calls = {}

    class FakeDecisionModel:
        def __init__(self, path, **kwargs):
            calls["load"] = (path, kwargs)

    monkeypatch.setitem(
        sys.modules,
        "frisket_models.classification._jeff_inference",
        SimpleNamespace(DecisionModel=FakeDecisionModel),
    )

    model = jeff._load_runtime(snapshot, cpu_threads=3)

    assert isinstance(model, FakeDecisionModel)
    assert calls["load"] == (
        snapshot,
        {
            "cpu_threads": 3,
            "local_files_only": True,
            "trust_remote_code": False,
        },
    )


def test_jeff_preserves_dotted_ids_duplicate_descriptions_and_order(
    monkeypatch, snapshot
):
    model = _JeffModel([0.2, 0.7, 0.1])
    monkeypatch.setattr(jeff, "_load_runtime", lambda *_args, **_kwargs: model)
    classifier = Jeff(snapshot)
    labels = ["z.last", "a.first", "middle"]

    result = classifier.classify(
        "A record",
        labels,
        descriptions={label: "same" for label in labels},
        instruction="Choose exactly one",
    )

    assert result == {
        "label": "a.first",
        "score": 0.7,
        "model_revision": snapshot.name,
    }
    rows, batch_size = model.calls[0]
    assert batch_size == 1
    question = rows[0]["question"]
    assert question["instructions"] == "Choose exactly one"
    assert list(question["criteria"]) == labels
    assert list(question["criteria"].values()) == ["same", "same", "same"]


def test_jeff_accepts_its_254_choice_options(monkeypatch, snapshot):
    probabilities = [0.0] * 254
    probabilities[-1] = 1.0
    model = _JeffModel(probabilities)
    monkeypatch.setattr(jeff, "_load_runtime", lambda *_args, **_kwargs: model)
    labels = [f"label-{index}" for index in range(254)]

    result = Jeff(snapshot).classify(
        "text", labels, descriptions={}, instruction="choose"
    )

    assert result["label"] == "label-253"


def test_jeff_propagates_native_combined_token_limit(monkeypatch, snapshot):
    class OverLimitModel:
        def predict(self, _rows, *, batch_size):
            assert batch_size == 1
            raise ValueError(
                "Question branch exceeds the 8192-token limit; no input was truncated."
            )

    monkeypatch.setattr(
        jeff, "_load_runtime", lambda *_args, **_kwargs: OverLimitModel()
    )

    with pytest.raises(ValueError, match="8192-token limit.*no input was truncated"):
        Jeff(snapshot).classify(
            "long text",
            ["one", "two"],
            descriptions={},
            instruction="choose",
        )


@pytest.mark.parametrize("adapter", [GLiClassBase, Jeff])
def test_adapters_reject_invalid_request_values(monkeypatch, snapshot, adapter):
    if adapter is GLiClassBase:
        monkeypatch.setattr(
            gliclass_base,
            "_load_runtime",
            lambda _path: (_GLiPipeline(), _GLiTokenizer()),
        )
    else:
        monkeypatch.setattr(
            jeff,
            "_load_runtime",
            lambda *_args, **_kwargs: _JeffModel([0.5, 0.5]),
        )
    classifier = adapter(snapshot)

    requests = [
        ("", ["one", "two"], {}, "choose"),
        ("text", ["one"], {}, "choose"),
        ("text", ["one", ""], {}, "choose"),
        ("text", ["one", "one"], {}, "choose"),
        ("text", ["one", "two"], {"other": "description"}, "choose"),
        ("text", ["one", "two"], {}, ""),
        (
            "text",
            [f"label-{index}" for index in range(255 if adapter is Jeff else 256)],
            {},
            "choose",
        ),
    ]
    for text, labels, descriptions, instruction in requests:
        with pytest.raises(ValueError):
            classifier.classify(
                text,
                labels,
                descriptions=descriptions,
                instruction=instruction,
            )


@pytest.mark.parametrize("adapter", [GLiClassBase, Jeff])
def test_adapters_reject_relative_snapshot(monkeypatch, tmp_path, adapter):
    monkeypatch.chdir(tmp_path)
    relative = tmp_path.name
    with pytest.raises(ValueError, match="absolute"):
        adapter(relative)


def test_gliclass_rejects_unknown_winner(monkeypatch, snapshot):
    class UnknownPipeline(_GLiPipeline):
        def __call__(self, text, labels, **kwargs):
            return [[{"label": "not-a-candidate", "score": 0.8}]]

    monkeypatch.setattr(
        gliclass_base,
        "_load_runtime",
        lambda _path: (UnknownPipeline(), _GLiTokenizer()),
    )

    with pytest.raises(ValueError, match="unknown label"):
        GLiClassBase(snapshot).classify(
            "text", ["one", "two"], descriptions={}, instruction="choose"
        )


@pytest.mark.parametrize("probabilities", [[0.5], [math.nan, 0.5], [1.1, -0.1]])
def test_jeff_rejects_invalid_native_distribution(monkeypatch, snapshot, probabilities):
    monkeypatch.setattr(
        jeff,
        "_load_runtime",
        lambda *_args, **_kwargs: _JeffModel(probabilities),
    )

    with pytest.raises(ValueError, match="probabilit"):
        Jeff(snapshot).classify(
            "text", ["one", "two"], descriptions={}, instruction="choose"
        )
