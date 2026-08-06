import numpy as np
import pytest

from classification_service.infrastructure.model.score_aggregator import (
    aggregate_scores,
)


def test_aggregates_chunk_probabilities_by_arithmetic_mean() -> None:
    scores = np.asarray([[0.8, 0.2], [0.4, 0.6]], dtype=np.float64)

    prediction = aggregate_scores(scores, ("first", "second"))

    assert prediction.label == "first"
    assert prediction.confidence == pytest.approx(0.6)


@pytest.mark.parametrize(
    "scores",
    [
        np.asarray([0.8, 0.2], dtype=np.float64),
        np.asarray([[[0.8, 0.2]]], dtype=np.float64),
        np.empty((0, 2), dtype=np.float64),
        np.asarray([[0.8], [0.4]], dtype=np.float64),
    ],
)
def test_rejects_score_matrices_with_invalid_shape(scores: np.ndarray) -> None:
    with pytest.raises(ValueError):
        aggregate_scores(scores, ("first", "second"))


@pytest.mark.parametrize("invalid_score", [np.nan, np.inf, -np.inf])
def test_rejects_non_finite_scores(invalid_score: float) -> None:
    scores = np.asarray([[invalid_score, 0.2]], dtype=np.float64)

    with pytest.raises(ValueError):
        aggregate_scores(scores, ("first", "second"))


@pytest.mark.parametrize("invalid_score", [-0.01, 1.01])
def test_rejects_scores_outside_probability_range(invalid_score: float) -> None:
    scores = np.asarray([[invalid_score, 0.2]], dtype=np.float64)

    with pytest.raises(ValueError):
        aggregate_scores(scores, ("first", "second"))


@pytest.mark.parametrize("labels", [(), ("",), ("same", "same")])
def test_rejects_empty_or_duplicate_labels(labels: tuple[str, ...]) -> None:
    scores = np.full((1, len(labels)), 0.5, dtype=np.float64)

    with pytest.raises(ValueError):
        aggregate_scores(scores, labels)


def test_probability_tie_uses_first_label_in_manifest_order() -> None:
    scores = np.asarray([[0.7, 0.3], [0.3, 0.7]], dtype=np.float64)

    prediction = aggregate_scores(scores, ("first", "second"))

    assert prediction.label == "first"
    assert prediction.confidence == pytest.approx(0.5)
