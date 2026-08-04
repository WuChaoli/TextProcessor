from collections.abc import Iterable

import pytest

from classification_service.infrastructure.model.tokenizer_chunker import (
    ChunkingConfig,
    TokenizerChunker,
)


class FakeTokenizer:
    def __init__(
        self,
        token_ids: Iterable[int],
        *,
        special_tokens: int = 2,
        empty_decode_starts: frozenset[int] = frozenset(),
    ) -> None:
        self.token_ids = list(token_ids)
        self.special_tokens = special_tokens
        self.empty_decode_starts = empty_decode_starts
        self.encoded_texts: list[str] = []
        self.decode_calls: list[tuple[int, ...]] = []

    def num_special_tokens_to_add(self, *, pair: bool) -> int:
        assert pair is False
        return self.special_tokens

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        self.encoded_texts.append(text)
        return list(self.token_ids)

    def decode(
        self,
        token_ids: Iterable[int],
        *,
        skip_special_tokens: bool,
        clean_up_tokenization_spaces: bool,
    ) -> str:
        assert skip_special_tokens is True
        assert clean_up_tokenization_spaces is False
        captured_ids = tuple(token_ids)
        self.decode_calls.append(captured_ids)
        if captured_ids and captured_ids[0] in self.empty_decode_starts:
            return ""
        return ",".join(str(token_id) for token_id in captured_ids)


def test_short_text_is_encoded_without_special_tokens_and_returned_as_one_chunk() -> (
    None
):
    tokenizer = FakeTokenizer([10, 11, 12])

    chunks = TokenizerChunker(tokenizer).chunk("short text")

    assert chunks == ("10,11,12",)
    assert tokenizer.encoded_texts == ["short text"]
    assert tokenizer.decode_calls == [(10, 11, 12)]


def test_special_tokens_are_subtracted_from_each_window_budget() -> None:
    tokenizer = FakeTokenizer(range(6), special_tokens=2)
    chunker = TokenizerChunker(
        tokenizer,
        ChunkingConfig(max_length=6, overlap=2, max_chunks_per_document=16),
    )

    chunks = chunker.chunk("six tokens")

    assert chunks == ("0,1,2,3", "2,3,4,5")
    assert tokenizer.decode_calls == [(0, 1, 2, 3), (2, 3, 4, 5)]


def test_default_windows_overlap_by_exactly_32_content_tokens() -> None:
    tokenizer = FakeTokenizer(range(476), special_tokens=2)

    TokenizerChunker(tokenizer).chunk("long text")

    first, second = tokenizer.decode_calls
    assert len(first) == 254
    assert len(second) == 254
    assert first[-32:] == second[:32]
    assert (first[0], second[0], second[-1]) == (0, 222, 475)


def test_more_than_16_windows_are_selected_uniformly_from_first_to_last() -> None:
    tokenizer = FakeTokenizer(range(4472), special_tokens=2)

    chunks = TokenizerChunker(tokenizer).chunk("very long text")

    expected_window_indices = (0, 1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14, 15, 16, 18, 19)
    expected_starts = tuple(index * 222 for index in expected_window_indices)
    assert len(chunks) == 16
    assert tuple(call[0] for call in tokenizer.decode_calls) == expected_starts
    assert expected_starts[0] == 0
    assert expected_starts[-1] == 4218


def test_empty_token_sequence_returns_no_chunks_without_decoding() -> None:
    tokenizer = FakeTokenizer([])

    chunks = TokenizerChunker(tokenizer).chunk("tokenizer considers this empty")

    assert chunks == ()
    assert tokenizer.decode_calls == []


def test_empty_decoded_windows_are_filtered_from_result() -> None:
    tokenizer = FakeTokenizer(range(8), empty_decode_starts=frozenset({0}))
    chunker = TokenizerChunker(
        tokenizer,
        ChunkingConfig(max_length=6, overlap=0, max_chunks_per_document=16),
    )

    chunks = chunker.chunk("two windows")

    assert tokenizer.decode_calls == [(0, 1, 2, 3), (4, 5, 6, 7)]
    assert chunks == ("4,5,6,7",)


@pytest.mark.parametrize(
    ("config", "special_tokens"),
    [
        (ChunkingConfig(max_length=0), 0),
        (ChunkingConfig(max_length=2), 2),
        (ChunkingConfig(max_length=6, overlap=-1), 2),
        (ChunkingConfig(max_length=6, overlap=4), 2),
        (ChunkingConfig(max_chunks_per_document=0), 2),
    ],
)
def test_invalid_effective_chunking_configuration_is_rejected(
    config: ChunkingConfig, special_tokens: int
) -> None:
    tokenizer = FakeTokenizer([1], special_tokens=special_tokens)

    with pytest.raises(ValueError):
        TokenizerChunker(tokenizer, config).chunk("text")
