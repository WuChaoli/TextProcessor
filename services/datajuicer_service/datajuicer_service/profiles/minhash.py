import hashlib
import struct
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

import numpy as np
from numpy.typing import NDArray
from scipy import integrate  # type: ignore[import-untyped]

from datajuicer_service.profiles.models import InputSample

MERSENNE_PRIME = np.uint64((1 << 61) - 1)
MAX_HASH = np.uint64((1 << 32) - 1)


@dataclass(frozen=True, slots=True)
class MinHashConfig:
    tokenization: str = "character"
    window_size: int = 5
    lowercase: bool = True
    ignore_pattern: str | None = None
    num_permutations: int = 256
    jaccard_threshold: float = 0.7

    @classmethod
    def v1(cls) -> "MinHashConfig":
        return cls()

    def __post_init__(self) -> None:
        if self.tokenization != "character":
            raise ValueError("v1 only supports character tokenization")
        if self.window_size <= 0 or self.num_permutations <= 0:
            raise ValueError("MinHash integer parameters must be positive")
        if not 0 <= self.jaccard_threshold <= 1:
            raise ValueError("jaccard_threshold must be between zero and one")
        if self.ignore_pattern is not None:
            raise ValueError("v1 does not support ignore_pattern")


@dataclass(frozen=True, slots=True)
class MinHashCluster:
    member_uids: tuple[int, ...]


def _sha1_hash32(data: bytes) -> int:
    return struct.unpack("<I", hashlib.sha1(data).digest()[:4])[0]


@cache
def _optimal_parameters(threshold: float, num_permutations: int) -> tuple[int, int]:
    def false_positive_probability(bands: int, rows: int) -> float:
        def probability(similarity: float) -> float:
            return 1 - (1 - similarity**float(rows)) ** float(bands)

        result, _ = integrate.quad(probability, 0.0, threshold)
        return float(result)

    def false_negative_probability(bands: int, rows: int) -> float:
        def probability(similarity: float) -> float:
            return (1 - similarity**float(rows)) ** float(bands)

        result, _ = integrate.quad(probability, threshold, 1.0)
        return float(result)

    minimum_error = float("inf")
    optimal = (0, 0)
    for bands in range(1, num_permutations + 1):
        for rows in range(1, num_permutations // bands + 1):
            error = (
                false_positive_probability(bands, rows)
                + false_negative_probability(bands, rows)
            ) * 0.5
            if error < minimum_error:
                minimum_error = error
                optimal = (bands, rows)
    return optimal


@cache
def _permutations(
    num_permutations: int,
) -> tuple[NDArray[np.uint64], NDArray[np.uint64]]:
    generator = np.random.RandomState(seed=42)
    perm_a, perm_b = np.array(
        [
            (
                generator.randint(1, MERSENNE_PRIME, dtype=np.uint64),
                generator.randint(0, MERSENNE_PRIME, dtype=np.uint64),
            )
            for _ in range(num_permutations)
        ],
        dtype=np.uint64,
    ).T
    return perm_a, perm_b


def _compute_signature(
    text: str,
    config: MinHashConfig,
) -> tuple[bytes, ...] | None:
    normalized = text.lower() if config.lowercase else text
    tokens = {
        normalized[index : index + config.window_size].encode()
        for index in range(len(normalized) - config.window_size + 1)
    }
    if not tokens:
        return None

    perm_a, perm_b = _permutations(config.num_permutations)
    minimum_hashes = np.full(config.num_permutations, MAX_HASH, dtype=np.uint64)
    token_hashes = np.fromiter(
        (_sha1_hash32(token) for token in tokens),
        dtype=np.uint64,
        count=len(tokens),
    )
    for start in range(0, len(token_hashes), 4096):
        values = token_hashes[start : start + 4096]
        permuted = np.bitwise_and(
            (values[:, None] * perm_a + perm_b) % MERSENNE_PRIME,
            MAX_HASH,
        )
        minimum_hashes = np.minimum(minimum_hashes, permuted.min(axis=0))

    num_bands, rows_per_band = _optimal_parameters(
        config.jaccard_threshold,
        config.num_permutations,
    )
    return tuple(
        minimum_hashes[start : start + rows_per_band].byteswap().tobytes()
        for start in range(0, num_bands * rows_per_band, rows_per_band)
    )


class _UnionFind:
    def __init__(self, uids: Sequence[int]) -> None:
        self._parent = {uid: uid for uid in uids}

    def find(self, uid: int) -> int:
        parent = self._parent[uid]
        if parent != uid:
            self._parent[uid] = self.find(parent)
        return self._parent[uid]

    def union(self, first: int, second: int) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root == second_root:
            return
        lower, higher = sorted((first_root, second_root))
        self._parent[higher] = lower


def cluster_minhash(
    samples: Sequence[InputSample],
    config: MinHashConfig,
) -> tuple[MinHashCluster, ...]:
    ordered_samples = sorted(samples, key=lambda sample: sample.uid)
    union_find = _UnionFind([sample.uid for sample in ordered_samples])
    buckets: dict[tuple[int, bytes], list[int]] = defaultdict(list)

    for sample in ordered_samples:
        signature = _compute_signature(sample.text, config)
        if signature is None:
            continue
        for band_index, band_hash in enumerate(signature):
            buckets[(band_index, band_hash)].append(sample.uid)

    for member_uids in buckets.values():
        if len(member_uids) <= 1:
            continue
        representative_uid = min(member_uids)
        for uid in member_uids:
            union_find.union(representative_uid, uid)

    components: dict[int, list[int]] = defaultdict(list)
    for sample in ordered_samples:
        components[union_find.find(sample.uid)].append(sample.uid)

    clusters = [
        MinHashCluster(member_uids=tuple(sorted(member_uids)))
        for member_uids in components.values()
        if len(member_uids) > 1
    ]
    clusters.sort(key=lambda cluster: cluster.member_uids)
    return tuple(clusters)
