"""Sensitive redaction with Presidio allowlist and deterministic fallback."""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.features.markdown_cleaning.processors.cn_recognizers import (
    CNIDCardRecognizer,
    CNMobilePhoneRecognizer,
    IPv4AddressRecognizer,
    is_valid_cn_id_card,
    is_valid_cn_mobile_phone,
    is_valid_credit_card,
    is_valid_ipv4_address,
)
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
    MarkdownCleaningProcessorError,
    map_processing_exception,
)
from app.features.markdown_cleaning.processors.models import SourceSpan

_HAS_PRESIDIO = False
_PRESIDIO_ANALYZER_ENGINE: Any = None
_PRESIDIO_PATTERN: Any = None
_PRESIDIO_PATTERN_RECOGNIZER: Any = None
_PRESIDIO_RECOGNIZER_REGISTRY: Any = None
_PRESIDIO_NOOP_NLP_ENGINE: Any = None
_PRESIDIO_CREDIT_CARD_RECOGNIZER: Any = None
_PRESIDIO_EMAIL_RECOGNIZER: Any = None

try:
    _presidio_module = importlib.import_module("presidio_analyzer")
    _PRESIDIO_ANALYZER_ENGINE = _presidio_module.AnalyzerEngine
    _PRESIDIO_PATTERN = _presidio_module.Pattern
    _PRESIDIO_PATTERN_RECOGNIZER = _presidio_module.PatternRecognizer
    _PRESIDIO_RECOGNIZER_REGISTRY = _presidio_module.RecognizerRegistry
    _PRESIDIO_NOOP_NLP_ENGINE = importlib.import_module(
        "presidio_analyzer.nlp_engine"
    ).NoOpNlpEngine
    _predefined = importlib.import_module("presidio_analyzer.predefined_recognizers")
    _PRESIDIO_CREDIT_CARD_RECOGNIZER = _predefined.CreditCardRecognizer
    _PRESIDIO_EMAIL_RECOGNIZER = _predefined.EmailRecognizer
    _HAS_PRESIDIO = True
except Exception:  # pragma: no cover - runtime compatibility path
    pass

AnalyzerEngine: Any = _PRESIDIO_ANALYZER_ENGINE
Pattern: Any = _PRESIDIO_PATTERN
PatternRecognizer: Any = _PRESIDIO_PATTERN_RECOGNIZER
RecognizerRegistry: Any = _PRESIDIO_RECOGNIZER_REGISTRY
EmailRecognizer: Any = _PRESIDIO_EMAIL_RECOGNIZER
CreditCardRecognizer: Any = _PRESIDIO_CREDIT_CARD_RECOGNIZER
NoOpNlpEngine: Any = _PRESIDIO_NOOP_NLP_ENGINE


_SUPPORTED_ENTITIES: tuple[str, ...] = (
    "EMAIL_ADDRESS",
    "CN_ID_CARD",
    "CN_MOBILE_PHONE",
    "CREDIT_CARD",
    "IPV4_ADDRESS",
)

_ENTITY_TO_TOKEN: dict[str, str] = {
    "EMAIL_ADDRESS": "[EMAIL]",
    "CN_ID_CARD": "[ID_CARD]",
    "CN_MOBILE_PHONE": "[PHONE]",
    "CREDIT_CARD": "[BANK_CARD]",
    "IPV4_ADDRESS": "[IPV4]",
}

_ENTITY_TO_SUMMARY_FIELD: dict[str, str] = {
    "EMAIL_ADDRESS": "email",
    "CN_ID_CARD": "id_card",
    "CN_MOBILE_PHONE": "phone",
    "CREDIT_CARD": "bank_card",
    "IPV4_ADDRESS": "ipv4",
}

_EMAIL_PATTERN = re.compile(
    r"(?<!\w)[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,63}(?!\w)"
)
_CN_ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_CN_MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9](?:[ -]?\d){9}(?!\d)")
_CREDIT_CARD_PATTERN = re.compile(r"(?<!\d)\d{12,19}(?!\d)")
_IPV4_PATTERN = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


if PatternRecognizer is not None and Pattern is not None:

    class _FallbackCreditCardRecognizer(PatternRecognizer):  # type: ignore[misc]
        """Fallback credit card recognizer when Presidio predefined one is absent."""

        def __init__(self) -> None:
            super().__init__(
                supported_entity="CREDIT_CARD",
                patterns=[Pattern("CREDIT_CARD", _CREDIT_CARD_PATTERN.pattern, 0.5)],
            )

        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_credit_card(pattern_text)


@dataclass(frozen=True, slots=True)
class SensitiveRedactionSummary:
    phone: int = 0
    id_card: int = 0
    bank_card: int = 0
    email: int = 0
    ipv4: int = 0


@dataclass(frozen=True, slots=True)
class SensitiveRedactionResult:
    text: str
    summary: SensitiveRedactionSummary
    used_fallback: bool
    fallback_error: str | None = None


@dataclass(frozen=True, slots=True)
class _SensitiveMatch:
    entity_type: str
    start: int
    end: int


class PresidioMarkdownRedactor:
    """Apply sensitive entity replacement for Markdown text."""

    def __init__(
        self,
        analyzer: Any = None,
        allowed_entities: tuple[str, ...] = _SUPPORTED_ENTITIES,
    ) -> None:
        self._allowed_entities = allowed_entities
        self._analyzer: Any = analyzer if analyzer is not None else self._build_analyzer()

    @staticmethod
    def _build_noop_nlp_engine() -> Any:
        if NoOpNlpEngine is None:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
                "NoOpNlpEngine 不可用",
            )
        return NoOpNlpEngine([{"lang_code": "en", "model_name": "noop-en"}])

    @staticmethod
    def _build_registry() -> Any:
        if RecognizerRegistry is None:
            raise MarkdownCleaningProcessorError(
                MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
                "RecognizerRegistry 不可用",
            )

        registry: Any = RecognizerRegistry()
        if EmailRecognizer is not None:
            registry.add_recognizer(EmailRecognizer())
        if CreditCardRecognizer is not None:
            registry.add_recognizer(CreditCardRecognizer())
        elif PatternRecognizer is not None and Pattern is not None:
            registry.add_recognizer(_FallbackCreditCardRecognizer())

        registry.add_recognizer(CNMobilePhoneRecognizer())
        registry.add_recognizer(CNIDCardRecognizer())
        registry.add_recognizer(IPv4AddressRecognizer())
        return registry

    def _build_analyzer(self) -> Any:
        if not _HAS_PRESIDIO or AnalyzerEngine is None:
            return None

        return AnalyzerEngine(
            registry=self._build_registry(),
            nlp_engine=self._build_noop_nlp_engine(),
        )

    def redact(
        self,
        markdown: str,
        protected_spans: Sequence[SourceSpan] | None = None,
    ) -> SensitiveRedactionResult:
        if not isinstance(markdown, str):
            raise map_processing_exception(
                TypeError("markdown 必须为字符串"),
                MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
            )

        protected = protected_spans or ()
        used_fallback = self._analyzer is None
        try:
            matches = tuple(self._iter_matches(markdown, protected))
            selected = self._resolve_overlaps(matches, protected)
            return SensitiveRedactionResult(
                text=self._replace_matches(markdown, selected),
                summary=self._summarize(selected),
                used_fallback=used_fallback,
            )
        except Exception as exc:
            mapped = map_processing_exception(
                exc,
                MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED,
            )
            if self._analyzer is not None:
                return SensitiveRedactionResult(
                    text=markdown,
                    summary=SensitiveRedactionSummary(),
                    used_fallback=False,
                    fallback_error=mapped.safe_message,
                )

            fallback_matches = self._fallback_matches(markdown, protected)
            selected = self._resolve_overlaps(fallback_matches, protected)
            return SensitiveRedactionResult(
                text=self._replace_matches(markdown, selected),
                summary=self._summarize(selected),
                used_fallback=True,
                fallback_error=mapped.safe_message,
            )

    def _iter_matches(
        self,
        markdown: str,
        protected_spans: Sequence[SourceSpan],
    ) -> Iterable[_SensitiveMatch]:
        if self._analyzer is None:
            return self._fallback_matches(markdown, protected_spans)

        raw_results = self._analyzer.analyze(
            text=markdown,
            entities=list(self._allowed_entities),
            language="en",
            score_threshold=0,
        )

        return tuple(
            _SensitiveMatch(result.entity_type, result.start, result.end)
            for result in raw_results
            if isinstance(result.entity_type, str)
            and result.entity_type in self._allowed_entities
            and self._is_valid_match(result, markdown)
            and _is_unprotected(result.start, result.end, protected_spans)
        )

    @staticmethod
    def _is_valid_match(result: object, markdown: str) -> bool:
        entity_type = getattr(result, "entity_type", None)
        start = getattr(result, "start", None)
        end = getattr(result, "end", None)
        if not isinstance(entity_type, str) or not isinstance(start, int) or not isinstance(end, int):
            return False
        if start < 0 or end <= start:
            return False
        text = markdown[start:end]
        if entity_type == "CREDIT_CARD":
            return is_valid_credit_card(text)
        if entity_type == "CN_MOBILE_PHONE":
            return is_valid_cn_mobile_phone(text)
        if entity_type == "CN_ID_CARD":
            return is_valid_cn_id_card(text)
        if entity_type == "IPV4_ADDRESS":
            return is_valid_ipv4_address(text)
        return True

    @staticmethod
    def _resolve_overlaps(
        matches: Sequence[_SensitiveMatch],
        protected_spans: Sequence[SourceSpan],
    ) -> tuple[_SensitiveMatch, ...]:
        selected: list[_SensitiveMatch] = []
        for entity_type in _SUPPORTED_ENTITIES:
            typed = sorted(
                (
                    match
                    for match in matches
                    if match.entity_type == entity_type
                    and _is_unprotected(match.start, match.end, protected_spans)
                ),
                key=lambda item: item.start,
            )
            for candidate in typed:
                if _has_overlap(candidate, selected):
                    continue
                selected.append(candidate)
        return tuple(sorted(selected, key=lambda item: item.start))

    @staticmethod
    def _replace_matches(markdown: str, matches: Sequence[_SensitiveMatch]) -> str:
        if not matches:
            return markdown

        output = markdown
        for match in sorted(matches, key=lambda item: item.start, reverse=True):
            output = (
                output[:match.start]
                + _ENTITY_TO_TOKEN[match.entity_type]
                + output[match.end :]
            )
        return output

    @staticmethod
    def _summarize(matches: Sequence[_SensitiveMatch]) -> SensitiveRedactionSummary:
        counts = {
            "phone": 0,
            "id_card": 0,
            "bank_card": 0,
            "email": 0,
            "ipv4": 0,
        }
        for match in matches:
            field = _ENTITY_TO_SUMMARY_FIELD.get(match.entity_type)
            if field is None:
                continue
            counts[field] = counts[field] + 1
        return SensitiveRedactionSummary(**counts)

    @staticmethod
    def _fallback_matches(
        markdown: str,
        protected_spans: Sequence[SourceSpan],
    ) -> tuple[_SensitiveMatch, ...]:
        results: list[_SensitiveMatch] = []

        for match in _EMAIL_PATTERN.finditer(markdown):
            if _is_unprotected(match.start(), match.end(), protected_spans):
                results.append(_SensitiveMatch("EMAIL_ADDRESS", match.start(), match.end()))

        for match in _CN_ID_CARD_PATTERN.finditer(markdown):
            value = match.group(0)
            if is_valid_cn_id_card(value) and _is_unprotected(
                match.start(), match.end(), protected_spans
            ):
                results.append(_SensitiveMatch("CN_ID_CARD", match.start(), match.end()))

        for match in _CN_MOBILE_PATTERN.finditer(markdown):
            value = match.group(0)
            if is_valid_cn_mobile_phone(value) and _is_unprotected(
                match.start(), match.end(), protected_spans
            ):
                results.append(
                    _SensitiveMatch("CN_MOBILE_PHONE", match.start(), match.end())
                )

        for match in _CREDIT_CARD_PATTERN.finditer(markdown):
            value = match.group(0)
            if is_valid_credit_card(value) and _is_unprotected(
                match.start(), match.end(), protected_spans
            ):
                results.append(_SensitiveMatch("CREDIT_CARD", match.start(), match.end()))

        for match in _IPV4_PATTERN.finditer(markdown):
            value = match.group(0)
            if is_valid_ipv4_address(value) and _is_unprotected(
                match.start(), match.end(), protected_spans
            ):
                results.append(_SensitiveMatch("IPV4_ADDRESS", match.start(), match.end()))

        return tuple(results)


def _is_unprotected(start: int, end: int, protected_spans: Sequence[SourceSpan]) -> bool:
    for protected in protected_spans:
        if protected.start < end and protected.end > start:
            return False
    return True


def _has_overlap(match: _SensitiveMatch, selected: Sequence[_SensitiveMatch]) -> bool:
    for selected_match in selected:
        if match.start < selected_match.end and match.end > selected_match.start:
            return True
    return False

