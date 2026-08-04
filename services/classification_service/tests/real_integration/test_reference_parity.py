import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytestmark = pytest.mark.real_integration


def _required_path(name: str) -> Path:
    value = os.getenv(name)
    if not value:
        pytest.skip(f"{name} is not configured")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{name} does not identify a controlled fixture file")
    return path


def test_reference_algorithm_and_service_are_numerically_equivalent() -> None:
    fixture_path = _required_path("CLASSIFICATION_REFERENCE_FIXTURE")
    fixture: dict[str, Any] = json.loads(fixture_path.read_text(encoding="utf-8"))
    text = fixture["text"]
    expected_chunk_ids = fixture["chunkIds"]
    expected_top = np.asarray(fixture["topProbabilities"], dtype=np.float64)
    expected_end = np.asarray(fixture["endDocProbabilities"], dtype=np.float64)

    from classification_service.domain.classification_result import ClassificationResult
    from classification_service.infrastructure.config import Settings
    from classification_service.infrastructure.model.runtime import (
        load_classification_runtime,
    )
    from classification_service.infrastructure.model.score_aggregator import (
        aggregate_scores,
    )
    from classification_service.infrastructure.model.tokenizer_chunker import (
        _uniform_indices,
        _window_starts,
    )
    from classification_service.infrastructure.release.validator import validate_release

    settings = Settings.model_validate({})
    release = validate_release(settings)
    runtime = load_classification_runtime(
        release, minimum_free_gpu_mib=settings.minimum_free_gpu_mib
    )
    chunker = runtime.chunker
    tokenizer = chunker._tokenizer  # noqa: SLF001 - real acceptance inspects the exact adapter input
    config = chunker._config  # noqa: SLF001
    all_ids = tokenizer.encode(text, add_special_tokens=False)
    budget = config.max_length - tokenizer.num_special_tokens_to_add(pair=False)
    starts = _window_starts(len(all_ids), budget, config.overlap)
    indices = _uniform_indices(len(starts), config.max_chunks_per_document)
    actual_chunk_ids = [
        all_ids[starts[index] : starts[index] + budget] for index in indices
    ]
    chunks = chunker.chunk(text)

    top_model = runtime.top_triple_classifier._model  # noqa: SLF001
    end_model = runtime.end_doc_classifier._model  # noqa: SLF001
    actual_top = np.asarray(
        top_model.predict_proba(list(chunks), as_numpy=True), dtype=np.float64
    )
    actual_end = np.asarray(
        end_model.predict_proba(list(chunks), as_numpy=True), dtype=np.float64
    )

    assert actual_chunk_ids == expected_chunk_ids
    np.testing.assert_allclose(actual_top, expected_top, rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(actual_end, expected_end, rtol=1e-6, atol=1e-8)
    np.testing.assert_allclose(
        actual_top.mean(axis=0), expected_top.mean(axis=0), rtol=1e-6, atol=1e-8
    )
    np.testing.assert_allclose(
        actual_end.mean(axis=0), expected_end.mean(axis=0), rtol=1e-6, atol=1e-8
    )

    top = aggregate_scores(actual_top, release.models["top-triple-classifier"].labels)
    end = aggregate_scores(actual_end, release.models["end-doc-classifier"].labels)
    result = ClassificationResult.compose(
        top_triple=top, end_doc=end, release_id=release.release_id
    )
    assert top.label == fixture["topLabel"]
    assert end.label == fixture["endDocLabel"]
    assert result.tags == tuple(fixture["tags"])
    assert result.top_triple_confidence == pytest.approx(
        fixture["topConfidence"], rel=1e-6, abs=1e-8
    )
    assert result.end_doc_confidence == pytest.approx(
        fixture["endDocConfidence"], rel=1e-6, abs=1e-8
    )
