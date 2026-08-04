import json
from pathlib import Path

import pytest

from classification_service.infrastructure.release.manifest import ReleaseManifest
from tools.package_release import package_release

TOP_MODEL = "top-triple-classifier"
END_MODEL = "end-doc-classifier"


def _manifest() -> ReleaseManifest:
    return ReleaseManifest.model_validate(
        {
            "schemaVersion": 1,
            "releaseId": "release-1",
            "qualityStatus": "experimental",
            "createdAt": "2026-08-04T00:00:00Z",
            "source": {"project": "trusted-training", "trainingRunId": "run-1"},
            "runtimeVersions": {
                "python": "3.11.13",
                "setfit": "1.1.3",
                "sentenceTransformers": "5.1.0",
                "transformers": "4.53.0",
                "torch": "2.7.1",
                "scikitLearn": "1.7.1",
            },
            "chunking": {
                "maxLength": 256,
                "overlap": 32,
                "maxChunksPerDocument": 16,
                "selection": "uniform",
            },
            "aggregation": "arithmetic_mean",
            "tokenizer": {"identity": "tokenizer-1", "path": "tokenizer"},
            "models": {
                TOP_MODEL: {
                    "path": f"{TOP_MODEL}/model",
                    "labels": [
                        f"top-{index} > middle-{index} > leaf-{index}"
                        for index in range(18)
                    ],
                    "labelCount": 18,
                    "metrics": {"accuracy": 0.54, "macroF1": 0.56},
                },
                END_MODEL: {
                    "path": f"{END_MODEL}/model",
                    "labels": [f"document-{index}" for index in range(6)],
                    "labelCount": 6,
                    "metrics": {"accuracy": 0.77, "macroF1": 0.77},
                },
            },
        }
    )


def _sources(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    tokenizer = tmp_path / "input-tokenizer"
    top = tmp_path / "input-top"
    end = tmp_path / "input-end"
    for directory, content in ((tokenizer, "tokenizer"), (top, "top"), (end, "end")):
        directory.mkdir()
        (directory / "artifact.txt").write_text(content, encoding="utf-8")
    return tokenizer, {TOP_MODEL: top, END_MODEL: end}


def test_package_release_refuses_existing_target(tmp_path: Path) -> None:
    tokenizer, model_sources = _sources(tmp_path)
    target = tmp_path / "release"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        package_release(
            target=target,
            tokenizer_source=tokenizer,
            model_sources=model_sources,
            manifest=_manifest(),
        )

    assert marker.read_text(encoding="utf-8") == "keep"
    assert (
        sorted(
            path.name
            for path in tmp_path.iterdir()
            if path.name.startswith(".release.")
        )
        == []
    )


def test_package_release_uses_complete_sibling_before_atomic_publish(
    tmp_path: Path,
) -> None:
    tokenizer, model_sources = _sources(tmp_path)
    target = tmp_path / "release"

    packaged = package_release(
        target=target,
        tokenizer_source=tokenizer,
        model_sources=model_sources,
        manifest=_manifest(),
    )

    assert packaged == target
    assert (
        json.loads((target / "manifest.json").read_text(encoding="utf-8"))["releaseId"]
        == "release-1"
    )
    checksum_text = (target / "checksums.sha256").read_text(encoding="utf-8")
    assert "tokenizer/artifact.txt" in checksum_text
    assert f"{TOP_MODEL}/model/artifact.txt" in checksum_text
    assert (
        sorted(
            path.name
            for path in tmp_path.iterdir()
            if path.name.startswith(".release.")
        )
        == []
    )


def test_failed_packaging_leaves_no_target_or_temporary_sibling(tmp_path: Path) -> None:
    tokenizer, model_sources = _sources(tmp_path)
    model_sources[END_MODEL] = tmp_path / "missing-model"
    target = tmp_path / "release"

    with pytest.raises(FileNotFoundError):
        package_release(
            target=target,
            tokenizer_source=tokenizer,
            model_sources=model_sources,
            manifest=_manifest(),
        )

    assert not target.exists()
    assert (
        sorted(
            path.name
            for path in tmp_path.iterdir()
            if path.name.startswith(".release.")
        )
        == []
    )
