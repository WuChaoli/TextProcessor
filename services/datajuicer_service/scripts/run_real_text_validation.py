from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx

from datajuicer_service.profiles.io import InputLimits
from datajuicer_service.profiles.text_exact_minhash_v1 import TextExactMinhashV1

DATASET_REPOSITORY = "fka/awesome-chatgpt-prompts"
RESOLVED_REPOSITORY = "fka/prompts.chat"
DATASET_REVISION = "c3064c383a935d8ff5b8d363888e317e4132badc"
DATASET_FILE = "prompts.csv"
SELECTED_INDICES = (0, 2, 9)
REQUEST_ID = "hf-real-validation-v1"
DATASET_URL = (
    f"https://huggingface.co/datasets/{DATASET_REPOSITORY}/resolve/"
    f"{DATASET_REVISION}/{DATASET_FILE}"
)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def download_dataset(cache_dir: Path) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{DATASET_REVISION}-{DATASET_FILE}"
    if cache_path.exists():
        return cache_path.read_bytes()

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        response = client.get(DATASET_URL)
        response.raise_for_status()
    content = response.content
    if len(content) > 2 * 1024 * 1024:
        raise RuntimeError("DATASET_FILE_TOO_LARGE")
    cache_path.write_bytes(content)
    return content


def read_source_rows(content: bytes) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise RuntimeError("DATASET_NOT_UTF8") from error
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or set(rows[0]) != {"act", "prompt"}:
        raise RuntimeError("UNEXPECTED_DATASET_SCHEMA")
    if max(SELECTED_INDICES) >= len(rows):
        raise RuntimeError("DATASET_SLICE_OUT_OF_RANGE")
    return rows


def build_fixture(rows: list[dict[str, str]]) -> list[dict[str, int | str]]:
    first = rows[SELECTED_INDICES[0]]["prompt"]
    second = rows[SELECTED_INDICES[1]]["prompt"]
    singleton = rows[SELECTED_INDICES[2]]["prompt"]
    if min(map(len, (first, second, singleton))) < 100:
        raise RuntimeError("DATASET_TEXT_TOO_SHORT")
    multilingual = second + "\n" + ("这是用于多语言近似去重验收的中文说明。" * 5)
    return [
        {"uid": 0, "text": first},
        {"uid": 1, "text": f"  {first}\n"},
        {"uid": 2, "text": first[:-1]},
        {"uid": 3, "text": singleton},
        {"uid": 4, "text": multilingual},
        {"uid": 5, "text": multilingual[:-1]},
    ]


def write_fixture(path: Path, records: list[dict[str, int | str]]) -> None:
    payload = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    ).encode("utf-8")
    path.write_bytes(b"\xef\xbb\xbf" + payload)


def summarize_output(path: Path) -> tuple[list[dict[str, Any]], list[int]]:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    singletons: list[int] = []
    for record in records:
        cluster_id = record["clusterId"]
        if cluster_id is None:
            singletons.append(record["uid"])
        else:
            grouped.setdefault(cluster_id, []).append(record)

    clusters = []
    for members in grouped.values():
        clusters.append(
            {
                "memberUids": sorted(member["uid"] for member in members),
                "method": members[0]["method"],
                "representativeUid": next(
                    member["uid"] for member in members if member["representative"]
                ),
            }
        )
    clusters.sort(key=lambda cluster: cluster["memberUids"])
    return clusters, sorted(singletons)


def run_validation(work_dir: Path, cache_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    source = download_dataset(cache_dir)
    rows = read_source_rows(source)
    fixture = build_fixture(rows)
    input_path = work_dir / "input.jsonl"
    output_path = work_dir / "output.jsonl"
    if input_path.exists() or output_path.exists():
        raise RuntimeError("VALIDATION_OUTPUT_CONFLICT")
    write_fixture(input_path, fixture)

    result = TextExactMinhashV1(
        InputLimits(
            max_records=100,
            max_bytes=2 * 1024 * 1024,
            max_text_chars=2 * 1024 * 1024,
        )
    ).execute(input_path, output_path, request_id=REQUEST_ID)
    clusters, singletons = summarize_output(output_path)
    return {
        "dataset": {
            "repository": DATASET_REPOSITORY,
            "resolvedRepository": RESOLVED_REPOSITORY,
            "revision": DATASET_REVISION,
            "file": DATASET_FILE,
            "sourceRecordCount": len(rows),
            "selectedIndices": list(SELECTED_INDICES),
        },
        "datasetSha256": sha256_bytes(source),
        "fixture": {
            "recordCount": len(fixture),
            "inputHadUtf8Bom": input_path.read_bytes().startswith(b"\xef\xbb\xbf"),
            "multilingualUids": [4, 5],
        },
        "inputPath": str(input_path.resolve()),
        "outputPath": str(output_path.resolve()),
        "inputSha256": result.input_sha256,
        "outputSha256": result.output_sha256,
        "clusters": clusters,
        "singletons": singletons,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = run_validation(args.work_dir, args.cache_dir)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(f"{args.report.resolve()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
