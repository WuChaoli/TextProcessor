import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import numpy as np
import pytest

from classification_service.domain.model_identity import (
    END_DOC_CLASSIFIER_NAME,
    TOP_TRIPLE_CLASSIFIER_NAME,
    ModelIdentity,
)
from classification_service.infrastructure.model.end_doc_classifier import (
    EndDocClassifier,
)
from classification_service.infrastructure.model.runtime import (
    load_classification_runtime,
)
from classification_service.infrastructure.model.setfit_loader import ModelLoadError
from classification_service.infrastructure.model.top_triple_classifier import (
    TopTripleClassifier,
)
from classification_service.infrastructure.release.manifest import ReleaseManifest
from classification_service.infrastructure.release.validator import (
    ValidatedModel,
    ValidatedRelease,
)

TOP_LABELS = tuple(
    f"top-{index} > middle-{index} > leaf-{index}" for index in range(18)
)
END_LABELS = tuple(f"document-{index}" for index in range(6))


class FakeTensor:
    def __init__(self, values: np.ndarray) -> None:
        self.values = values
        self.detach_calls = 0
        self.cpu_calls = 0

    def detach(self) -> "FakeTensor":
        self.detach_calls += 1
        return self

    def cpu(self) -> "FakeTensor":
        self.cpu_calls += 1
        return self

    def numpy(self) -> np.ndarray:
        return self.values


class FakePredictModel:
    def __init__(self, probabilities: object) -> None:
        self.probabilities = probabilities
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def predict_proba(self, chunks: Sequence[str], *, as_numpy: bool = True) -> object:
        self.calls.append((tuple(chunks), as_numpy))
        return self.probabilities


def _one_hot_probabilities(
    chunk_count: int, label_count: int, selected_index: int
) -> np.ndarray:
    probabilities = np.zeros((chunk_count, label_count), dtype=np.float64)
    probabilities[:, selected_index] = 1.0
    return probabilities


def test_top_triple_adapter_normalizes_tensor_and_uses_manifest_label_order() -> None:
    manifest_labels = tuple(reversed(TOP_LABELS))
    tensor = FakeTensor(_one_hot_probabilities(2, 18, 4))
    model = FakePredictModel(tensor)

    prediction = TopTripleClassifier(model, manifest_labels).predict(
        ("first chunk", "second chunk")
    )

    assert prediction.label == manifest_labels[4]
    assert prediction.confidence == pytest.approx(1.0)
    assert model.calls == [(("first chunk", "second chunk"), True)]
    assert (tensor.detach_calls, tensor.cpu_calls) == (1, 1)


def test_end_doc_adapter_accepts_ndarray_and_uses_manifest_label_order() -> None:
    manifest_labels = tuple(reversed(END_LABELS))
    model = FakePredictModel(_one_hot_probabilities(2, 6, 2))

    prediction = EndDocClassifier(model, manifest_labels).predict(
        ("first chunk", "second chunk")
    )

    assert prediction.label == manifest_labels[2]
    assert prediction.confidence == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("adapter", "label_count", "invalid_shape"),
    [
        (TopTripleClassifier, 18, (1, 17)),
        (TopTripleClassifier, 18, (2, 18, 1)),
        (EndDocClassifier, 6, (1, 5)),
        (EndDocClassifier, 6, (2, 6, 1)),
    ],
)
def test_adapters_reject_probability_shape_that_does_not_match_chunks_and_labels(
    adapter: type[TopTripleClassifier] | type[EndDocClassifier],
    label_count: int,
    invalid_shape: tuple[int, ...],
) -> None:
    labels = TOP_LABELS if label_count == 18 else END_LABELS
    model = FakePredictModel(np.zeros(invalid_shape, dtype=np.float64))

    with pytest.raises(ValueError, match="shape"):
        adapter(model, labels).predict(("first chunk", "second chunk"))


@pytest.mark.parametrize(
    ("adapter", "labels"),
    [(TopTripleClassifier, TOP_LABELS), (EndDocClassifier, END_LABELS)],
)
def test_adapters_reject_nan_probabilities(
    adapter: type[TopTripleClassifier] | type[EndDocClassifier],
    labels: tuple[str, ...],
) -> None:
    probabilities = _one_hot_probabilities(1, len(labels), 0)
    probabilities[0, 0] = np.nan

    with pytest.raises(ValueError, match="finite"):
        adapter(FakePredictModel(probabilities), labels).predict(("chunk",))


@pytest.mark.parametrize(
    ("adapter", "labels"),
    [(TopTripleClassifier, TOP_LABELS), (EndDocClassifier, END_LABELS)],
)
def test_adapters_reject_rows_that_are_not_probability_distributions(
    adapter: type[TopTripleClassifier] | type[EndDocClassifier],
    labels: tuple[str, ...],
) -> None:
    probabilities = np.full((1, len(labels)), 0.25, dtype=np.float64)

    with pytest.raises(ValueError, match="sum"):
        adapter(FakePredictModel(probabilities), labels).predict(("chunk",))


class FakeTokenizer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def num_special_tokens_to_add(self, *, pair: bool) -> int:
        assert pair is False
        return 2

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert text
        assert add_special_tokens is False
        self.events.append("tokenize-smoke")
        return [10, 11, 12]

    def decode(
        self,
        token_ids: Iterable[int],
        *,
        skip_special_tokens: bool,
        clean_up_tokenization_spaces: bool,
    ) -> str:
        assert tuple(token_ids) == (10, 11, 12)
        assert skip_special_tokens is True
        assert clean_up_tokenization_spaces is False
        return "fixed smoke chunk"


class FakeRuntimeModel:
    def __init__(self, name: str, label_count: int, events: list[str]) -> None:
        self.name = name
        self.label_count = label_count
        self.events = events

    def predict_proba(
        self, chunks: Sequence[str], *, as_numpy: bool = True
    ) -> np.ndarray:
        assert as_numpy is True
        self.events.append(f"predict-{self.name}")
        return _one_hot_probabilities(len(chunks), self.label_count, 0)


class FakeInferenceMode:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> None:
        self.events.append("inference-mode-enter")

    def __exit__(self, *args: object) -> None:
        self.events.append("inference-mode-exit")


def _release(tmp_path: Path) -> ValidatedRelease:
    root = tmp_path / "release"
    tokenizer_path = root / "tokenizer"
    top_path = root / TOP_TRIPLE_CLASSIFIER_NAME / "model"
    end_path = root / END_DOC_CLASSIFIER_NAME / "model"
    tokenizer_path.mkdir(parents=True)
    top_path.mkdir(parents=True)
    end_path.mkdir(parents=True)
    manifest = ReleaseManifest.model_validate(
        {
            "schemaVersion": 1,
            "releaseId": "release-1",
            "qualityStatus": "production-approved",
            "createdAt": "2026-08-04T00:00:00Z",
            "source": {"project": "trusted-training", "trainingRunId": "run-1"},
            "runtimeVersions": {
                "python": "3.12.12",
                "setfit": "1.1.3",
                "sentenceTransformers": "5.6.1",
                "transformers": "4.49.0",
                "torch": "2.13.0",
                "scikitLearn": "1.9.0",
            },
            "chunking": {
                "maxLength": 256,
                "overlap": 32,
                "maxChunksPerDocument": 16,
                "selection": "uniform",
            },
            "aggregation": "arithmetic_mean",
            "tokenizer": {"identity": "tokenizer-1", "path": "tokenizer"},
            "models": {
                TOP_TRIPLE_CLASSIFIER_NAME: {
                    "path": f"{TOP_TRIPLE_CLASSIFIER_NAME}/model",
                    "labels": TOP_LABELS,
                    "labelCount": 18,
                    "metrics": {"accuracy": 0.54, "macroF1": 0.56},
                },
                END_DOC_CLASSIFIER_NAME: {
                    "path": f"{END_DOC_CLASSIFIER_NAME}/model",
                    "labels": END_LABELS,
                    "labelCount": 6,
                    "metrics": {"accuracy": 0.77, "macroF1": 0.77},
                },
            },
        }
    )
    return ValidatedRelease(
        release_id="release-1",
        root=root,
        tokenizer_path=tokenizer_path,
        models=MappingProxyType(
            {
                TOP_TRIPLE_CLASSIFIER_NAME: ValidatedModel(
                    identity=ModelIdentity(
                        name=TOP_TRIPLE_CLASSIFIER_NAME,
                        release_id="release-1",
                    ),
                    path=top_path,
                    labels=TOP_LABELS,
                ),
                END_DOC_CLASSIFIER_NAME: ValidatedModel(
                    identity=ModelIdentity(
                        name=END_DOC_CLASSIFIER_NAME,
                        release_id="release-1",
                    ),
                    path=end_path,
                    labels=END_LABELS,
                ),
            }
        ),
        manifest=manifest,
    )


def _install_fake_modules(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    cuda_available: bool = True,
    device_count: int = 1,
    free_mib: int = 8192,
    fail_model: str | None = None,
    invalid_smoke: bool = False,
) -> None:
    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return cuda_available

        @staticmethod
        def device_count() -> int:
            return device_count

        @staticmethod
        def mem_get_info(device: str) -> tuple[int, int]:
            assert device == "cuda:0"
            return free_mib * 1024 * 1024, 24 * 1024 * 1024 * 1024

    class FakeAutoTokenizer:
        @classmethod
        def from_pretrained(
            cls,
            path: str,
            *,
            local_files_only: bool,
            trust_remote_code: bool,
        ) -> FakeTokenizer:
            assert local_files_only is True
            assert trust_remote_code is False
            events.append(f"load-tokenizer:{Path(path).name}")
            return FakeTokenizer(events)

    class FakeSetFitModel:
        @classmethod
        def from_pretrained(
            cls,
            path: str,
            *,
            device: str,
            local_files_only: bool,
        ) -> FakeRuntimeModel:
            assert device == "cuda:0"
            assert local_files_only is True
            name = Path(path).parent.name
            events.append(f"load-model:{name}:{device}")
            if fail_model == name:
                raise RuntimeError("fake load failure with an internal path")
            label_count = 18 if name == TOP_TRIPLE_CLASSIFIER_NAME else 6
            model = FakeRuntimeModel(name, label_count, events)
            if invalid_smoke and name == END_DOC_CLASSIFIER_NAME:
                model.label_count = 5
            return model

    fake_torch = SimpleNamespace(
        cuda=FakeCuda(), inference_mode=lambda: FakeInferenceMode(events)
    )
    fake_transformers = SimpleNamespace(AutoTokenizer=FakeAutoTokenizer)
    fake_setfit = SimpleNamespace(SetFitModel=FakeSetFitModel)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    monkeypatch.setitem(sys.modules, "setfit", fake_setfit)


def test_runtime_enforces_cuda_then_loads_assets_in_fixed_order_and_smokes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_modules(monkeypatch, events)

    runtime = load_classification_runtime(_release(tmp_path), minimum_free_gpu_mib=8192)

    assert runtime.release_id == "release-1"
    assert runtime.device == "cuda:0"
    assert events == [
        "load-tokenizer:tokenizer",
        f"load-model:{TOP_TRIPLE_CLASSIFIER_NAME}:cuda:0",
        f"load-model:{END_DOC_CLASSIFIER_NAME}:cuda:0",
        "tokenize-smoke",
        "inference-mode-enter",
        f"predict-{TOP_TRIPLE_CLASSIFIER_NAME}",
        f"predict-{END_DOC_CLASSIFIER_NAME}",
        "inference-mode-exit",
    ]


@pytest.mark.parametrize(
    ("cuda_available", "device_count", "free_mib"),
    [(False, 1, 8192), (True, 0, 8192), (True, 2, 8192), (True, 1, 8191)],
)
def test_runtime_fails_closed_before_loading_assets_when_cuda_gate_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cuda_available: bool,
    device_count: int,
    free_mib: int,
) -> None:
    events: list[str] = []
    _install_fake_modules(
        monkeypatch,
        events,
        cuda_available=cuda_available,
        device_count=device_count,
        free_mib=free_mib,
    )

    with pytest.raises(ModelLoadError) as caught:
        load_classification_runtime(_release(tmp_path), minimum_free_gpu_mib=8192)

    assert caught.value.code == "MODEL_LOAD_FAILED"
    assert caught.value.stage == "checking_cuda"
    assert caught.value.release_id == "release-1"
    assert events == []


def test_runtime_wraps_model_loading_failure_without_exposing_internal_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_modules(monkeypatch, events, fail_model=TOP_TRIPLE_CLASSIFIER_NAME)

    with pytest.raises(ModelLoadError) as caught:
        load_classification_runtime(_release(tmp_path), minimum_free_gpu_mib=8192)

    assert caught.value.stage == "loading_top_triple_classifier"
    assert "internal path" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert all(END_DOC_CLASSIFIER_NAME not in event for event in events)


def test_runtime_wraps_invalid_smoke_output_as_model_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    _install_fake_modules(monkeypatch, events, invalid_smoke=True)

    with pytest.raises(ModelLoadError) as caught:
        load_classification_runtime(_release(tmp_path), minimum_free_gpu_mib=8192)

    assert caught.value.stage == "smoke_testing"
    assert events[-1] == "inference-mode-exit"
