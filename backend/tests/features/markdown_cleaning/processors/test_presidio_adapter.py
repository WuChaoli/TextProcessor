from __future__ import annotations

import pytest

import app.features.markdown_cleaning.processors.presidio_adapter as presidio_adapter
from app.features.markdown_cleaning.processors.errors import (
    MarkdownCleaningErrorCode,
)
from app.features.markdown_cleaning.processors.models import SourceSpan
from app.features.markdown_cleaning.processors.presidio_adapter import (
    MarkdownCleaningProcessorError,
    PresidioMarkdownRedactor,
    SensitiveRedactionSummary,
)


class _DummyResult:
    def __init__(self, entity_type: str, start: int, end: int) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end


def test_main_path_redacts_all_supported_entities_and_preserves_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DummyAnalyzer:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def analyze(self, **kwargs: object) -> list[_DummyResult]:
            self.calls.append(kwargs)
            markdown = kwargs["text"]
            assert isinstance(markdown, str)
            return [
                _DummyResult("CN_ID_CARD", markdown.index("11010519491231002X"), markdown.index("11010519491231002X") + 18),
                _DummyResult("CREDIT_CARD", markdown.index("4111111111110006"), markdown.index("4111111111110006") + 16),
                _DummyResult("CN_MOBILE_PHONE", markdown.index("13800138000"), markdown.index("13800138000") + 11),
                _DummyResult("EMAIL_ADDRESS", markdown.index("a@sample.org"), markdown.index("a@sample.org") + 12),
                _DummyResult("IPV4_ADDRESS", markdown.index("10.0.0.1"), markdown.index("10.0.0.1") + 8),
            ]

    markdown = (
        "a@sample.org 11010519491231002X 13800138000 4111111111110006 "
        "10.0.0.1 and another 13800138000"
    )
    analyzer = _DummyAnalyzer()
    monkeypatch.setattr(presidio_adapter, "AnalyzerEngine", lambda **__: analyzer)
    monkeypatch.setattr(presidio_adapter, "_HAS_PRESIDIO", True)
    redactor = PresidioMarkdownRedactor(analyzer=analyzer)
    result = redactor.redact(markdown)

    assert analyzer.calls
    assert analyzer.calls[0]["entities"] == list(presidio_adapter._SUPPORTED_ENTITIES)
    assert result.used_fallback is False
    assert result.fallback_error is None
    assert result.text == (
        "[EMAIL] [ID_CARD] [PHONE] [BANK_CARD] [IPV4] and another 13800138000"
    )
    assert result.summary == SensitiveRedactionSummary(
        phone=1, id_card=1, bank_card=1, email=1, ipv4=1
    )


def test_redact_runtime_analyze_error_raises_processor_error() -> None:
    class _FailingAnalyzer:
        def analyze(self, *_: object, **__: object) -> list[object]:
            raise RuntimeError("analyzer fail")

    markdown = "contact:13800138000"
    with pytest.raises(MarkdownCleaningProcessorError) as exc:
        PresidioMarkdownRedactor(analyzer=_FailingAnalyzer()).redact(markdown)

    assert exc.value.code == MarkdownCleaningErrorCode.SENSITIVE_DATA_REDACTION_FAILED
    assert exc.value.safe_message == "敏感数据脱敏失败"
    assert str(exc.value) == "SENSITIVE_DATA_REDACTION_FAILED: 敏感数据脱敏失败"


def test_main_path_filters_invalid_spans_and_keeps_valid_text_and_summary() -> None:
    markdown = (
        "bad@ a@sample.org 13800138000 11010519491231002X 10.0.0.1 256.1.1.1"
    )

    email_start = markdown.index("a@sample.org")
    phone_start = markdown.index("13800138000")
    id_start = markdown.index("11010519491231002X")
    ipv4_start = markdown.index("10.0.0.1")

    class _FakeAnalyzer:
        def analyze(self, **_: object) -> list[_DummyResult]:
            return [
                _DummyResult("EMAIL_ADDRESS", 0, 4),
                _DummyResult("EMAIL_ADDRESS", email_start, email_start + len("a@sample.org")),
                _DummyResult("CN_MOBILE_PHONE", phone_start + 1, phone_start + 10),
                _DummyResult("CN_MOBILE_PHONE", phone_start, phone_start + 11),
                _DummyResult("CN_ID_CARD", id_start, id_start + 18),
                _DummyResult("IPV4_ADDRESS", ipv4_start, ipv4_start + 8),
                _DummyResult("IPV4_ADDRESS", 999, 1002),
                _DummyResult("CN_MOBILE_PHONE", -1, 5),
            ]

    result = PresidioMarkdownRedactor(analyzer=_FakeAnalyzer()).redact(markdown)
    assert result.used_fallback is False
    assert result.text == (
        "bad@ [EMAIL] [PHONE] [ID_CARD] [IPV4] 256.1.1.1"
    )
    assert result.summary == SensitiveRedactionSummary(
        phone=1, id_card=1, bank_card=0, email=1, ipv4=1
    )


def test_fallback_uses_only_valid_matches_and_respects_protected_and_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presidio_adapter, "_HAS_PRESIDIO", False)
    monkeypatch.setattr(presidio_adapter, "AnalyzerEngine", None)
    markdown = (
        "a@sample.org 11010519491231002X 13800138000 4111111111110006 "
        "10.0.0.1 11010519491231002X 13800138000 10.1.1.1 256.1.1.1 bad-email@  "
        "4111111111111112"
    )
    phone_start = markdown.index("13800138000")
    protected = (SourceSpan(start=phone_start, end=phone_start + 11),)
    result = PresidioMarkdownRedactor(analyzer=None).redact(markdown, protected_spans=protected)

    assert result.used_fallback is True
    assert result.fallback_error is None
    assert result.text == (
        "[EMAIL] [ID_CARD] 13800138000 [BANK_CARD] [IPV4] [ID_CARD] [PHONE] [IPV4] 256.1.1.1 bad-email@  "
        "4111111111111112"
    )
    assert result.summary == SensitiveRedactionSummary(
        phone=1, id_card=2, bank_card=1, email=1, ipv4=2
    )


def test_overlap_resolution_uses_supported_entity_order() -> None:
    class _OverlappingAnalyzer:
        def analyze(self, *_: object, **__: object) -> list[_DummyResult]:
            return [
                _DummyResult(entity_type="CN_MOBILE_PHONE", start=0, end=11),
                _DummyResult(entity_type="CREDIT_CARD", start=0, end=11),
            ]

    result = PresidioMarkdownRedactor(analyzer=_OverlappingAnalyzer()).redact("13800138000")
    assert result.text == "[PHONE]"
    assert result.summary.phone == 1
    assert result.summary.bank_card == 0

def test_presidio_main_path_uses_noop_nlp_and_allowlist_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not presidio_adapter._HAS_PRESIDIO:
        pytest.skip("presidio_analyzer unavailable")

    class _DummyNoOpNlpEngine:
        def __init__(self, models: list[dict[str, str]]) -> None:
            self.models = models

    class _DummyRegistry:
        def __init__(self) -> None:
            self.names: list[str] = []
            self.supported_languages = ["en"]

        def add_recognizer(self, recognizer: object) -> None:
            self.names.append(type(recognizer).__name__)

    class _DummyAnalyzer:
        def __init__(self, registry: _DummyRegistry, nlp_engine: _DummyNoOpNlpEngine) -> None:
            self.registry = registry
            self.nlp_engine = nlp_engine
            self.calls: list[dict[str, object]] = []

        def analyze(self, **kwargs: object) -> list[_DummyResult]:
            self.calls.append(kwargs)
            return []

    class _DummyEmailRecognizer:
        pass

    class _DummyCreditCardRecognizer:
        pass

    def _create_analyzer(
        *,
        registry: _DummyRegistry,
        nlp_engine: _DummyNoOpNlpEngine,
        **_: object,
    ) -> _DummyAnalyzer:
        return _DummyAnalyzer(registry, nlp_engine)

    monkeypatch.setattr(presidio_adapter, "RecognizerRegistry", _DummyRegistry)
    monkeypatch.setattr(presidio_adapter, "NoOpNlpEngine", _DummyNoOpNlpEngine)
    monkeypatch.setattr(presidio_adapter, "AnalyzerEngine", _create_analyzer)
    monkeypatch.setattr(presidio_adapter, "EmailRecognizer", _DummyEmailRecognizer)
    monkeypatch.setattr(presidio_adapter, "CreditCardRecognizer", _DummyCreditCardRecognizer)

    redactor = PresidioMarkdownRedactor(analyzer=None)
    analyzer = redactor._analyzer
    assert isinstance(analyzer, _DummyAnalyzer)
    assert analyzer.nlp_engine.models == [{"lang_code": "en", "model_name": "noop-en"}]
    redactor.redact("a@sample.org 13800138000")

    registry = analyzer.registry
    assert "_DummyEmailRecognizer" in registry.names
    assert "_DummyCreditCardRecognizer" in registry.names
    assert "_FallbackCreditCardRecognizer" not in registry.names
    assert registry.names == [
        "_DummyEmailRecognizer",
        "_DummyCreditCardRecognizer",
        "CNMobilePhoneRecognizer",
        "CNIDCardRecognizer",
        "IPv4AddressRecognizer",
    ]
    assert len(analyzer.calls) == 1
    assert analyzer.calls[0]["entities"] == list(presidio_adapter._SUPPORTED_ENTITIES)


def test_fallback_registry_registers_credit_card_recognizer_when_builtin_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not presidio_adapter._HAS_PRESIDIO:
        pytest.skip("presidio_analyzer unavailable")

    class _DummyRegistry:
        def __init__(self) -> None:
            self.names: list[str] = []

        def add_recognizer(self, recognizer: object) -> None:
            self.names.append(type(recognizer).__name__)

    monkeypatch.setattr(presidio_adapter, "RecognizerRegistry", _DummyRegistry)
    monkeypatch.setattr(presidio_adapter, "EmailRecognizer", None)
    monkeypatch.setattr(presidio_adapter, "CreditCardRecognizer", None)

    registry = presidio_adapter.PresidioMarkdownRedactor._build_registry()
    assert "_FallbackEmailRecognizer" in registry.names
    assert "_FallbackCreditCardRecognizer" in registry.names
