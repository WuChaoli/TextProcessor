import asyncio
from collections.abc import Callable
from typing import TypeVar

import pytest

from classification_service.application.classify_text import ClassifyTextHandler
from classification_service.application.dto import ClassifyTextCommand
from classification_service.domain.model_identity import ModelPrediction

T = TypeVar("T")


class InlineInferenceExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, operation: Callable[[], T]) -> T:
        self.calls += 1
        return operation()


class RecordingChunker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[str] = []
        self.chunks = ("first chunk", "second chunk")

    def chunk(self, text: str) -> tuple[str, ...]:
        self.events.append("chunk")
        self.calls.append(text)
        return self.chunks


class RecordingClassifier:
    def __init__(
        self,
        name: str,
        prediction: ModelPrediction,
        events: list[str],
        *,
        failure: Exception | None = None,
    ) -> None:
        self.name = name
        self.prediction = prediction
        self.events = events
        self.failure = failure
        self.calls: list[tuple[str, ...]] = []

    def predict(self, chunks: tuple[str, ...]) -> ModelPrediction:
        self.events.append(self.name)
        self.calls.append(chunks)
        if self.failure is not None:
            raise self.failure
        return self.prediction


def _handler(
    *,
    chunker: RecordingChunker,
    top_classifier: RecordingClassifier,
    end_classifier: RecordingClassifier,
    executor: InlineInferenceExecutor,
) -> ClassifyTextHandler:
    return ClassifyTextHandler(
        chunker=chunker,
        top_triple_classifier=top_classifier,
        end_doc_classifier=end_classifier,
        release_id="release-1",
        executor=executor,
    )


def test_classify_chunks_once_then_runs_top_triple_before_end_doc() -> None:
    events: list[str] = []
    chunker = RecordingChunker(events)
    top_classifier = RecordingClassifier(
        "top", ModelPrediction("应急 > 安全生产 > 危化品", 0.72), events
    )
    end_classifier = RecordingClassifier(
        "end", ModelPrediction("法规标准类", 0.81), events
    )
    executor = InlineInferenceExecutor()

    result = asyncio.run(
        _handler(
            chunker=chunker,
            top_classifier=top_classifier,
            end_classifier=end_classifier,
            executor=executor,
        ).execute(ClassifyTextCommand(request_id="request-1", text="raw body"))
    )

    assert result.tags == ("应急", "安全生产", "危化品", "法规标准类")
    assert result.release_id == "release-1"
    assert chunker.calls == ["raw body"]
    assert top_classifier.calls == [chunker.chunks]
    assert end_classifier.calls == [chunker.chunks]
    assert events == ["chunk", "top", "end"]
    assert executor.calls == 1


@pytest.mark.parametrize("failing_classifier", ["top", "end"])
def test_classify_propagates_any_pipeline_failure_without_a_partial_result(
    failing_classifier: str,
) -> None:
    events: list[str] = []
    chunker = RecordingChunker(events)
    top_classifier = RecordingClassifier(
        "top",
        ModelPrediction("应急 > 安全生产 > 危化品", 0.72),
        events,
        failure=(RuntimeError("top failed") if failing_classifier == "top" else None),
    )
    end_classifier = RecordingClassifier(
        "end",
        ModelPrediction("法规标准类", 0.81),
        events,
        failure=(RuntimeError("end failed") if failing_classifier == "end" else None),
    )
    handler = _handler(
        chunker=chunker,
        top_classifier=top_classifier,
        end_classifier=end_classifier,
        executor=InlineInferenceExecutor(),
    )

    with pytest.raises(RuntimeError, match=f"{failing_classifier} failed"):
        asyncio.run(
            handler.execute(
                ClassifyTextCommand(request_id="request-1", text="raw body")
            )
        )

    expected_events = ["chunk", "top"]
    if failing_classifier == "end":
        expected_events.append("end")
    assert events == expected_events
