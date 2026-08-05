"""Project-local Presidio recognizers and validation helpers for CN-sensitive data."""

from __future__ import annotations

import importlib
import re
from datetime import datetime
from typing import Any, cast

_HAS_PRESIDIO = False
_PRESIDIO_PATTERN: Any = None
_PRESIDIO_PATTERN_RECOGNIZER: Any = None

try:
    _presidio_module = importlib.import_module("presidio_analyzer")
except Exception:  # pragma: no cover - runtime compatibility path
    pass
else:
    _HAS_PRESIDIO = True
    _PRESIDIO_PATTERN = _presidio_module.Pattern
    _PRESIDIO_PATTERN_RECOGNIZER = _presidio_module.PatternRecognizer

Pattern = _PRESIDIO_PATTERN
PatternRecognizer = _PRESIDIO_PATTERN_RECOGNIZER
PatternRecognizerBase: type[Any] = cast(type[Any], PatternRecognizer)

_MOBILE_PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9](?:[ -]?\d){9}(?!\d)")
_ID_CARD_PATTERN = re.compile(r"(?<!\d)(\d{17}[Xx\d])(?!\d)")
_CREDIT_CARD_PATTERN = re.compile(r"(?<!\d)(\d{12,19})(?!\d)")
_IPV4_PATTERN = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")

_GB11643_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_GB11643_CHECK_SUM_CHARS = "10X98765432"
_GB11643_PROVINCES = {
    11,
    12,
    13,
    14,
    15,
    21,
    22,
    23,
    31,
    32,
    33,
    34,
    35,
    36,
    37,
    41,
    42,
    43,
    44,
    45,
    46,
    50,
    51,
    52,
    53,
    54,
    61,
    62,
    63,
    64,
    65,
    71,
    81,
    82,
}


def normalize_chinese_phone(phone: str) -> str:
    return re.sub(r"[ -]", "", phone)


def is_valid_cn_mobile_phone(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if not _MOBILE_PHONE_PATTERN.fullmatch(value):
        return False

    digits = normalize_chinese_phone(value)
    return bool(re.fullmatch(r"1[3-9]\d{9}", digits))


def is_valid_cn_id_card(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 18:
        return False

    upper_value = value.upper()
    if not _ID_CARD_PATTERN.fullmatch(upper_value):
        return False

    try:
        province = int(upper_value[:2])
    except ValueError:
        return False
    if province not in _GB11643_PROVINCES:
        return False
    if upper_value[2:6] == "0000":
        return False
    try:
        datetime.strptime(upper_value[6:14], "%Y%m%d")
    except ValueError:
        return False

    weighted_sum = 0
    for char, weight in zip(upper_value[:17], _GB11643_WEIGHTS, strict=True):
        weighted_sum += int(char) * weight
    return upper_value[-1] == _GB11643_CHECK_SUM_CHARS[weighted_sum % 11]


def is_valid_credit_card(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if not _CREDIT_CARD_PATTERN.fullmatch(value):
        return False

    digits = value.replace(" ", "")
    if not digits.isdigit() or not (12 <= len(digits) <= 19):
        return False

    odd = True
    total = 0
    for ch in reversed(digits):
        digit = int(ch)
        if odd:
            total += digit
        else:
            doubled = digit * 2
            total += doubled - 9 if doubled > 9 else doubled
        odd = not odd
    return total % 10 == 0


def is_valid_ipv4_address(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if not _IPV4_PATTERN.fullmatch(value):
        return False

    try:
        octets = [int(part) for part in value.split(".")]
    except ValueError:
        return False
    return all(0 <= octet <= 255 for octet in octets)


CNMobilePhoneRecognizer: type[Any]
CNIDCardRecognizer: type[Any]
IPv4AddressRecognizer: type[Any]

if (
    _HAS_PRESIDIO
    and _PRESIDIO_PATTERN_RECOGNIZER is not None
    and _PRESIDIO_PATTERN is not None
):

    class _PresidioCNMobilePhoneRecognizer(PatternRecognizerBase):  # type: ignore[misc]
        """Match China mobile phone numbers with controlled separator support."""

        def __init__(self) -> None:
            super().__init__(
                supported_entity="CN_MOBILE_PHONE",
                patterns=[
                    _PRESIDIO_PATTERN(
                        "CN_MOBILE_PHONE", _MOBILE_PHONE_PATTERN.pattern, 0.5
                    )
                ],
            )

        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_cn_mobile_phone(pattern_text)

    class _PresidioCNIDCardRecognizer(PatternRecognizerBase):  # type: ignore[misc]
        """Match 18-digit China Resident ID cards with basic GB11643 checks."""

        def __init__(self) -> None:
            super().__init__(
                supported_entity="CN_ID_CARD",
                patterns=[
                    _PRESIDIO_PATTERN("CN_ID_CARD", _ID_CARD_PATTERN.pattern, 0.5)
                ],
            )

        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_cn_id_card(pattern_text)

    class _PresidioIPv4AddressRecognizer(PatternRecognizerBase):  # type: ignore[misc]
        """Match IPv4 addresses with strict octet validation."""

        def __init__(self) -> None:
            super().__init__(
                supported_entity="IPV4_ADDRESS",
                patterns=[
                    _PRESIDIO_PATTERN("IPV4_ADDRESS", _IPV4_PATTERN.pattern, 0.5)
                ],
            )

        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_ipv4_address(pattern_text)

    CNMobilePhoneRecognizer = _PresidioCNMobilePhoneRecognizer
    CNIDCardRecognizer = _PresidioCNIDCardRecognizer
    IPv4AddressRecognizer = _PresidioIPv4AddressRecognizer
else:

    class _FallbackCNMobilePhoneRecognizer:
        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_cn_mobile_phone(pattern_text)

    class _FallbackCNIDCardRecognizer:
        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_cn_id_card(pattern_text)

    class _FallbackIPv4AddressRecognizer:
        def validate_result(self, pattern_text: str) -> bool:
            return is_valid_ipv4_address(pattern_text)

    CNMobilePhoneRecognizer = _FallbackCNMobilePhoneRecognizer
    CNIDCardRecognizer = _FallbackCNIDCardRecognizer
    IPv4AddressRecognizer = _FallbackIPv4AddressRecognizer

CNMobilePhoneRecognizer.__name__ = "CNMobilePhoneRecognizer"
CNIDCardRecognizer.__name__ = "CNIDCardRecognizer"
IPv4AddressRecognizer.__name__ = "IPv4AddressRecognizer"
