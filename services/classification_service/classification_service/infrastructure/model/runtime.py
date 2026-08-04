from contextlib import AbstractContextManager
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast

from classification_service.domain.classification_result import ClassificationResult
from classification_service.domain.model_identity import (
    END_DOC_CLASSIFIER_NAME,
    TOP_TRIPLE_CLASSIFIER_NAME,
)
from classification_service.infrastructure.model.end_doc_classifier import (
    EndDocClassifier,
)
from classification_service.infrastructure.model.setfit_loader import (
    ModelLoadError,
    load_setfit_model,
)
from classification_service.infrastructure.model.tokenizer_chunker import (
    Tokenizer,
    TokenizerChunker,
)
from classification_service.infrastructure.model.top_triple_classifier import (
    TopTripleClassifier,
)
from classification_service.infrastructure.release.validator import ValidatedRelease

CUDA_DEVICE = "cuda:0"
SMOKE_TEXT = "classification runtime readiness smoke"


class CudaRuntime(Protocol):
    def is_available(self) -> bool: ...

    def device_count(self) -> int: ...

    def mem_get_info(self, device: str) -> tuple[int, int]: ...


class TorchRuntime(Protocol):
    cuda: CudaRuntime

    def inference_mode(self) -> AbstractContextManager[None]: ...


class TokenizerFactory(Protocol):
    def from_pretrained(
        self,
        path: str,
        *,
        local_files_only: bool,
        trust_remote_code: bool,
    ) -> object: ...


class TransformersModule(Protocol):
    AutoTokenizer: TokenizerFactory


@dataclass(frozen=True)
class LoadedClassificationRuntime:
    release_id: str
    device: str
    chunker: TokenizerChunker
    top_triple_classifier: TopTripleClassifier
    end_doc_classifier: EndDocClassifier


def _model_load_error(stage: str, release_id: str) -> ModelLoadError:
    return ModelLoadError(stage=stage, release_id=release_id)


def _check_cuda(torch: TorchRuntime, minimum_free_gpu_mib: int) -> None:
    if minimum_free_gpu_mib <= 0:
        raise ValueError("minimum free GPU memory must be positive")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available")
    if torch.cuda.device_count() != 1:
        raise RuntimeError("exactly one logical CUDA device is required")
    free_bytes, _ = torch.cuda.mem_get_info(CUDA_DEVICE)
    required_bytes = minimum_free_gpu_mib * 1024 * 1024
    if free_bytes < required_bytes:
        raise RuntimeError("free CUDA memory is below the startup minimum")


def load_classification_runtime(
    release: ValidatedRelease, *, minimum_free_gpu_mib: int = 8192
) -> LoadedClassificationRuntime:
    """Load and smoke-test one immutable release without a CPU fallback."""
    release_id = release.release_id
    try:
        torch = cast(TorchRuntime, import_module("torch"))
        setfit = import_module("setfit")
        transformers = cast(TransformersModule, import_module("transformers"))
    except Exception:
        raise _model_load_error("importing_runtime", release_id) from None

    try:
        _check_cuda(torch, minimum_free_gpu_mib)
    except Exception:
        raise _model_load_error("checking_cuda", release_id) from None

    try:
        tokenizer_value = transformers.AutoTokenizer.from_pretrained(
            str(release.tokenizer_path),
            local_files_only=True,
            trust_remote_code=False,
        )
        tokenizer = cast(Tokenizer, tokenizer_value)
        chunker = TokenizerChunker(tokenizer)
    except Exception:
        raise _model_load_error("loading_tokenizer", release_id) from None

    top_release = release.models[TOP_TRIPLE_CLASSIFIER_NAME]
    try:
        top_model = load_setfit_model(setfit, top_release.path, device=CUDA_DEVICE)
        top_classifier = TopTripleClassifier(top_model, top_release.labels)
    except Exception:
        raise _model_load_error("loading_top_triple_classifier", release_id) from None

    end_release = release.models[END_DOC_CLASSIFIER_NAME]
    try:
        end_model = load_setfit_model(setfit, end_release.path, device=CUDA_DEVICE)
        end_classifier = EndDocClassifier(end_model, end_release.labels)
    except Exception:
        raise _model_load_error("loading_end_doc_classifier", release_id) from None

    try:
        chunks = chunker.chunk(SMOKE_TEXT)
        if not chunks or len(chunks) > 16:
            raise ValueError("smoke chunks violate the runtime contract")
        with torch.inference_mode():
            top_prediction = top_classifier.predict(chunks)
            end_prediction = end_classifier.predict(chunks)
        ClassificationResult.compose(
            top_triple=top_prediction,
            end_doc=end_prediction,
            release_id=release_id,
        )
    except Exception:
        raise _model_load_error("smoke_testing", release_id) from None

    return LoadedClassificationRuntime(
        release_id=release_id,
        device=CUDA_DEVICE,
        chunker=chunker,
        top_triple_classifier=top_classifier,
        end_doc_classifier=end_classifier,
    )
