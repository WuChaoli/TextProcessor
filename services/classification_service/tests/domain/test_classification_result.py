import math

import pytest

from classification_service.domain.classification_result import ClassificationResult
from classification_service.domain.errors import DomainValidationError
from classification_service.domain.label_path import TopTriplePath
from classification_service.domain.model_identity import ModelIdentity, ModelPrediction


def test_compose_fixed_four_tags() -> None:
    result = ClassificationResult.compose(
        top_triple=ModelPrediction("应急 > 安全生产 > 危化品", 0.72),
        end_doc=ModelPrediction("法规标准类", 0.81),
        release_id="release-1",
    )

    assert result.tags == ("应急", "安全生产", "危化品", "法规标准类")
    assert result.top_triple_confidence == 0.72
    assert result.end_doc_confidence == 0.81
    assert result.release_id == "release-1"


@pytest.mark.parametrize(
    "label",
    ["", "应急 > 安全生产", "a > b > c > d", "应急 >  > 危化品"],
)
def test_top_triple_requires_exactly_three_non_empty_levels(label: str) -> None:
    with pytest.raises(DomainValidationError):
        TopTriplePath.from_leaf_label(label)


@pytest.mark.parametrize("confidence", [math.nan, math.inf, -math.inf, -0.01, 1.01])
def test_model_prediction_rejects_non_finite_or_out_of_range_confidence(
    confidence: float,
) -> None:
    with pytest.raises(DomainValidationError):
        ModelPrediction("法规标准类", confidence)


def test_model_prediction_rejects_empty_label() -> None:
    with pytest.raises(DomainValidationError):
        ModelPrediction("", 0.81)


def test_compose_rejects_empty_end_doc_label() -> None:
    with pytest.raises(DomainValidationError):
        ClassificationResult.compose(
            top_triple=ModelPrediction("应急 > 安全生产 > 危化品", 0.72),
            end_doc=ModelPrediction("", 0.81),
            release_id="release-1",
        )


def test_domain_types_are_immutable() -> None:
    prediction = ModelPrediction("法规标准类", 0.81)
    path = TopTriplePath.from_leaf_label("应急 > 安全生产 > 危化品")
    result = ClassificationResult.compose(
        top_triple=ModelPrediction("应急 > 安全生产 > 危化品", 0.72),
        end_doc=prediction,
        release_id="release-1",
    )

    with pytest.raises(AttributeError):
        prediction.label = "其他类"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        path.levels = ("a", "b", "c")  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.release_id = "release-2"  # type: ignore[misc]


@pytest.mark.parametrize("name", ["A", "B", "other-classifier"])
def test_model_identity_rejects_non_production_classifier_name(name: str) -> None:
    with pytest.raises(DomainValidationError):
        ModelIdentity(name=name, release_id="release-1")


def test_mutating_source_lists_does_not_change_domain_values() -> None:
    levels = ["应急", "安全生产", "危化品"]
    tags = [*levels, "法规标准类"]
    path = TopTriplePath(levels=levels)  # type: ignore[arg-type]
    result = ClassificationResult(
        tags=tags,  # type: ignore[arg-type]
        top_triple_confidence=0.72,
        end_doc_confidence=0.81,
        release_id="release-1",
    )

    levels[0] = "已变更"
    tags[3] = "已变更"

    assert path.levels == ("应急", "安全生产", "危化品")
    assert result.tags == ("应急", "安全生产", "危化品", "法规标准类")
