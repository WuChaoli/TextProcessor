from typing import Any

import numpy as np
from numpy.typing import NDArray

from classification_service.domain.model_identity import ModelPrediction


def aggregate_scores(
    scores: NDArray[np.floating[Any]], labels: tuple[str, ...]
) -> ModelPrediction:
    """Average chunk probabilities and select the first maximum label."""
    if scores.ndim != 2:
        raise ValueError("scores must be a two-dimensional matrix")
    if not labels or any(not label for label in labels):
        raise ValueError("labels must contain only non-empty values")
    if len(set(labels)) != len(labels):
        raise ValueError("labels must be unique")
    if scores.shape[0] == 0 or scores.shape[1] != len(labels):
        raise ValueError("scores shape must be (chunk_count, label_count)")
    if not np.isfinite(scores).all():
        raise ValueError("scores must contain only finite values")
    if (scores < 0.0).any() or (scores > 1.0).any():
        raise ValueError("scores must be probabilities in [0, 1]")

    mean_scores = np.asarray(np.mean(scores, axis=0, dtype=np.float64))
    selected_index = int(np.argmax(mean_scores))
    return ModelPrediction(
        label=labels[selected_index], confidence=float(mean_scores[selected_index])
    )
