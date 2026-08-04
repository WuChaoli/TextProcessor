import os
from importlib import import_module

import pytest

pytestmark = pytest.mark.real_integration

REQUIRED_ENVIRONMENT = (
    "CLASSIFICATION_ENVIRONMENT",
    "CLASSIFICATION_INTERNAL_SERVICE_TOKEN",
    "CLASSIFICATION_MODEL_ROOT",
    "CLASSIFICATION_MODEL_RELEASE",
    "CLASSIFICATION_MODEL_RELEASE_SHA256",
    "CLASSIFICATION_RELEASE_QUALITY_STATUS",
)


def test_validated_release_loads_and_runs_both_models_on_one_rtx_3090() -> None:
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.getenv(name)]
    if missing:
        pytest.skip("real classification release environment is not configured")

    torch = import_module("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA is not available")
    if torch.cuda.device_count() != 1:
        pytest.skip("exactly one logical CUDA device is required")
    if "RTX 3090" not in torch.cuda.get_device_name("cuda:0"):
        pytest.skip("the logical CUDA device is not an RTX 3090")

    from classification_service.infrastructure.config import Settings
    from classification_service.infrastructure.model.runtime import (
        load_classification_runtime,
    )
    from classification_service.infrastructure.release.validator import (
        validate_release,
    )

    settings = Settings.model_validate({})
    release = validate_release(settings)
    runtime = load_classification_runtime(
        release, minimum_free_gpu_mib=settings.minimum_free_gpu_mib
    )
    chunks = runtime.chunker.chunk("用于分类服务真实加载验证的固定非敏感中文文本。")

    with torch.inference_mode():
        top_prediction = runtime.top_triple_classifier.predict(chunks)
        end_prediction = runtime.end_doc_classifier.predict(chunks)

    assert runtime.release_id == release.release_id
    assert runtime.device == "cuda:0"
    assert top_prediction.label in release.models["top-triple-classifier"].labels
    assert end_prediction.label in release.models["end-doc-classifier"].labels
