from datajuicer_service.profiles.minhash import (
    MinHashCluster,
    MinHashConfig,
    cluster_minhash,
)
from datajuicer_service.profiles.models import InputSample


def test_minhash_clusters_chinese_near_duplicates() -> None:
    base_text = "这是用于测试的中文长文档，包含完整的主体内容和结论。" * 20
    samples = [
        InputSample(uid=0, text=base_text),
        InputSample(uid=1, text=f"{base_text}附"),
        InputSample(uid=2, text="完全不同的技术说明。" * 20),
    ]

    clusters = cluster_minhash(samples, MinHashConfig.v1())

    assert clusters == (MinHashCluster(member_uids=(0, 1)),)


def test_minhash_result_is_independent_of_sample_order() -> None:
    base_text = "A deterministic near duplicate document. " * 30
    samples = [
        InputSample(uid=10, text=base_text),
        InputSample(uid=2, text=f"{base_text}!"),
        InputSample(uid=7, text="Unrelated content about databases. " * 30),
    ]

    forward = cluster_minhash(samples, MinHashConfig.v1())
    reverse = cluster_minhash(list(reversed(samples)), MinHashConfig.v1())

    expected = (MinHashCluster(member_uids=(2, 10)),)
    assert forward == expected
    assert reverse == expected


def test_minhash_v1_lowercases_text() -> None:
    samples = [
        InputSample(uid=0, text="Mixed CASE content for similarity. " * 20),
        InputSample(uid=1, text="mixed case content for similarity. " * 20),
    ]

    assert cluster_minhash(samples, MinHashConfig.v1()) == (
        MinHashCluster(member_uids=(0, 1)),
    )


def test_minhash_does_not_cluster_text_shorter_than_window() -> None:
    samples = [
        InputSample(uid=0, text="甲"),
        InputSample(uid=1, text="乙"),
        InputSample(uid=2, text=""),
    ]

    assert cluster_minhash(samples, MinHashConfig.v1()) == ()
