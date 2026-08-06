from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class Tokenizer(Protocol):
    """Tokenizer operations required by the production chunker."""

    def num_special_tokens_to_add(self, *, pair: bool) -> int: ...

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...

    def decode(
        self,
        token_ids: Sequence[int],
        *,
        skip_special_tokens: bool,
        clean_up_tokenization_spaces: bool,
    ) -> str: ...


@dataclass(frozen=True)
class ChunkingConfig:
    max_length: int = 256
    overlap: int = 32
    max_chunks_per_document: int = 16


def _window_starts(
    token_count: int, content_budget: int, overlap: int
) -> tuple[int, ...]:
    if token_count <= content_budget:
        return (0,)

    step = content_budget - overlap
    last_start = token_count - content_budget
    starts = list(range(0, last_start + 1, step))
    if starts[-1] != last_start:
        starts.append(last_start)
    return tuple(starts)


def _uniform_indices(total: int, limit: int) -> tuple[int, ...]:
    if total <= limit:
        return tuple(range(total))
    if limit == 1:
        return (0,)
    return tuple(round(index * (total - 1) / (limit - 1)) for index in range(limit))


class TokenizerChunker:
    """Deterministically chunk text according to the model token budget."""

    def __init__(
        self, tokenizer: Tokenizer, config: ChunkingConfig = ChunkingConfig()
    ) -> None:
        self._tokenizer = tokenizer
        self._config = config

    def chunk(self, text: str) -> tuple[str, ...]:
        special_tokens = self._tokenizer.num_special_tokens_to_add(pair=False)
        content_budget = self._config.max_length - special_tokens
        self._validate(content_budget)

        token_ids = self._tokenizer.encode(text, add_special_tokens=False)
        if not token_ids:
            return ()

        starts = _window_starts(len(token_ids), content_budget, self._config.overlap)
        selected_indices = _uniform_indices(
            len(starts), self._config.max_chunks_per_document
        )

        chunks: list[str] = []
        for selected_index in selected_indices:
            start = starts[selected_index]
            decoded = self._tokenizer.decode(
                token_ids[start : start + content_budget],
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False,
            )
            if decoded.strip():
                chunks.append(decoded)
        return tuple(chunks)

    def _validate(self, content_budget: int) -> None:
        if self._config.max_length <= 0:
            raise ValueError("max_length must be positive")
        if content_budget <= 0:
            raise ValueError("special tokens leave no content token budget")
        if self._config.overlap < 0 or self._config.overlap >= content_budget:
            raise ValueError("overlap must be in [0, content token budget)")
        if self._config.max_chunks_per_document <= 0:
            raise ValueError("max_chunks_per_document must be positive")
