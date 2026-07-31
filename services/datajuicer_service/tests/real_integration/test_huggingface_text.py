import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_integration


def test_pinned_huggingface_text_slice_has_expected_clusters(tmp_path: Path) -> None:
    if os.environ.get("DATAJUICER_RUN_REAL_HF") != "1":
        pytest.skip("DATAJUICER_RUN_REAL_HF=1 is required")

    service_root = Path(__file__).resolve().parents[2]
    report_path = tmp_path / "report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(service_root / "scripts" / "run_real_text_validation.py"),
            "--work-dir",
            str(tmp_path / "work"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--report",
            str(report_path),
        ],
        cwd=service_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["dataset"] == {
        "repository": "fka/awesome-chatgpt-prompts",
        "resolvedRepository": "fka/prompts.chat",
        "revision": "c3064c383a935d8ff5b8d363888e317e4132badc",
        "file": "prompts.csv",
        "sourceRecordCount": 203,
        "selectedIndices": [0, 2, 9],
    }
    assert report["fixture"]["recordCount"] == 6
    assert report["fixture"]["inputHadUtf8Bom"] is True
    assert report["fixture"]["multilingualUids"] == [4, 5]
    assert report["clusters"] == [
        {
            "memberUids": [0, 1, 2],
            "method": "exact_minhash",
            "representativeUid": 0,
        },
        {
            "memberUids": [4, 5],
            "method": "minhash",
            "representativeUid": 4,
        },
    ]
    assert report["singletons"] == [3]
    assert len(report["inputSha256"]) == 64
    assert len(report["outputSha256"]) == 64
