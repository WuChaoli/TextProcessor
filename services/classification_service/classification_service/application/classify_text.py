from classification_service.application.dto import ClassifyTextCommand
from classification_service.application.ports.classifier import Classifier
from classification_service.application.ports.inference_executor import (
    InferenceExecutor,
)
from classification_service.application.ports.text_chunker import TextChunker
from classification_service.domain.classification_result import ClassificationResult


class ClassifyTextHandler:
    """Execute the complete deterministic classification pipeline once."""

    def __init__(
        self,
        *,
        chunker: TextChunker,
        top_triple_classifier: Classifier,
        end_doc_classifier: Classifier,
        release_id: str,
        executor: InferenceExecutor,
    ) -> None:
        self._chunker = chunker
        self._top_triple_classifier = top_triple_classifier
        self._end_doc_classifier = end_doc_classifier
        self._release_id = release_id
        self._executor = executor

    async def execute(self, command: ClassifyTextCommand) -> ClassificationResult:
        return await self._executor.run(lambda: self._classify_blocking(command))

    def _classify_blocking(self, command: ClassifyTextCommand) -> ClassificationResult:
        chunks = self._chunker.chunk(command.text)
        top_triple = self._top_triple_classifier.predict(chunks)
        end_doc = self._end_doc_classifier.predict(chunks)
        return ClassificationResult.compose(
            top_triple=top_triple,
            end_doc=end_doc,
            release_id=self._release_id,
        )
