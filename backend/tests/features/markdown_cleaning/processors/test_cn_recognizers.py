from app.features.markdown_cleaning.processors.cn_recognizers import (
    is_valid_cn_id_card,
    is_valid_cn_mobile_phone,
    is_valid_credit_card,
    is_valid_ipv4_address,
    normalize_chinese_phone,
)


def test_normalize_chinese_phone_and_validate_mobile_phone() -> None:
    assert normalize_chinese_phone("138 0013-8000") == "13800138000"
    assert is_valid_cn_mobile_phone("13800138000")
    assert is_valid_cn_mobile_phone("138 0013 8000")
    assert is_valid_cn_mobile_phone("138-0013-8000")
    assert not is_valid_cn_mobile_phone("11100138000")
    assert not is_valid_cn_mobile_phone("1380013800")
    assert not is_valid_cn_mobile_phone("23800138000")


def test_validate_chinese_id_card_checksum_and_format() -> None:
    assert is_valid_cn_id_card("11010519491231002X")
    assert not is_valid_cn_id_card("110105194912310021")
    assert not is_valid_cn_id_card("99010519491231002X")
    assert not is_valid_cn_id_card("11010519491231X002X")


def test_validate_credit_card_and_ipv4() -> None:
    assert is_valid_credit_card("4111111111110006")
    assert not is_valid_credit_card("4111111111111112")
    assert not is_valid_credit_card("4111-1111-1111-0006")
    assert is_valid_ipv4_address("192.168.0.1")
    assert not is_valid_ipv4_address("256.1.1.1")
    assert not is_valid_ipv4_address("1.2.3")
