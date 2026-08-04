import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from classification_service.infrastructure.config import Settings
from classification_service.infrastructure.release.manifest import ReleaseManifest
from classification_service.infrastructure.release.validator import validate_release

TOP_MODEL = "top-triple-classifier"
END_MODEL = "end-doc-classifier"
TOP_LABELS = tuple(
    f"top-{index} > middle-{index} > leaf-{index}" for index in range(18)
)
END_LABELS = tuple(f"document-{index}" for index in range(6))
RUNTIME_VERSIONS = {
    "python": "3.11.13",
    "setfit": "1.1.3",
    "sentenceTransformers": "5.1.0",
    "transformers": "4.53.0",
    "torch": "2.7.1",
    "scikitLearn": "1.7.1",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(
    *,
    quality_status: str = "production-approved",
    top_labels: tuple[str, ...] = TOP_LABELS,
    end_labels: tuple[str, ...] = END_LABELS,
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "releaseId": "release-1",
        "qualityStatus": quality_status,
        "createdAt": "2026-08-04T00:00:00Z",
        "source": {"project": "trusted-training", "trainingRunId": "run-1"},
        "runtimeVersions": RUNTIME_VERSIONS,
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
                "labels": list(top_labels),
                "labelCount": len(top_labels),
                "metrics": {"accuracy": 0.54, "macroF1": 0.56},
            },
            END_MODEL: {
                "path": f"{END_MODEL}/model",
                "labels": list(end_labels),
                "labelCount": len(end_labels),
                "metrics": {"accuracy": 0.77, "macroF1": 0.77},
            },
        },
    }


def _write_release(
    root: Path,
    *,
    quality_status: str = "production-approved",
    top_labels: tuple[str, ...] = TOP_LABELS,
    end_labels: tuple[str, ...] = END_LABELS,
) -> Path:
    (root / "tokenizer").mkdir(parents=True)
    (root / TOP_MODEL / "model").mkdir(parents=True)
    (root / END_MODEL / "model").mkdir(parents=True)
    (root / "tokenizer" / "tokenizer.json").write_text("tokenizer", encoding="utf-8")
    (root / TOP_MODEL / "model" / "weights.txt").write_text("top", encoding="utf-8")
    (root / END_MODEL / "model" / "weights.txt").write_text("end", encoding="utf-8")
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                quality_status=quality_status,
                top_labels=top_labels,
                end_labels=end_labels,
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    checksum_lines = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    ]
    (root / "checksums.sha256").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    return manifest_path


def _settings(
    root: Path,
    manifest_path: Path,
    *,
    environment: str = "development",
    release_quality_status: str = "production-approved",
) -> Settings:
    return Settings(
        environment=environment,
        internal_service_token=SecretStr("secret"),
        model_root=root.parent,
        model_release=root,
        model_release_sha256=_sha256(manifest_path),
        release_quality_status=release_quality_status,
    )


def test_manifest_schema_forbids_unknown_fields(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["unexpected"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReleaseManifest.load(path)


def test_rejects_configured_manifest_sha256_mismatch(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    settings = _settings(release, manifest_path)
    settings.model_release_sha256 = "0" * 64

    with pytest.raises(ValueError, match="manifest SHA256"):
        validate_release(settings)


def test_rejects_missing_registered_file(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    (release / TOP_MODEL / "model" / "weights.txt").unlink()

    with pytest.raises(ValueError, match="missing registered file"):
        validate_release(_settings(release, manifest_path))


def test_rejects_unregistered_file(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    (release / "unregistered.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ValueError, match="unregistered file"):
        validate_release(_settings(release, manifest_path))


def test_rejects_checksum_mismatch(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    (release / "tokenizer" / "tokenizer.json").write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_release(_settings(release, manifest_path))


def test_rejects_non_canonical_checksum_path(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    checksum_path = release / "checksums.sha256"
    checksum_path.write_text(
        checksum_path.read_text(encoding="utf-8").replace(
            "tokenizer/tokenizer.json", "tokenizer\\tokenizer.json"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="canonical POSIX"):
        validate_release(_settings(release, manifest_path))


def test_rejects_symlink_that_escapes_release(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = release / "escape.txt"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    checksum_path = release / "checksums.sha256"
    checksum_path.write_text(
        checksum_path.read_text(encoding="utf-8") + f"{_sha256(outside)}  escape.txt\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="symbolic link"):
        validate_release(_settings(release, manifest_path))


@pytest.mark.parametrize(
    ("top_labels", "end_labels"),
    [
        (TOP_LABELS[:-1], END_LABELS),
        (TOP_LABELS, END_LABELS[:-1]),
    ],
)
def test_rejects_wrong_classifier_label_count(
    tmp_path: Path, top_labels: tuple[str, ...], end_labels: tuple[str, ...]
) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(
        release, top_labels=top_labels, end_labels=end_labels
    )

    with pytest.raises(ValidationError, match="labels"):
        validate_release(_settings(release, manifest_path))


def test_rejects_top_label_that_is_not_three_levels(tmp_path: Path) -> None:
    release = tmp_path / "release"
    invalid_labels = ("only > two", *TOP_LABELS[1:])
    manifest_path = _write_release(release, top_labels=invalid_labels)

    with pytest.raises(ValidationError, match="three-level"):
        validate_release(_settings(release, manifest_path))


def test_production_rejects_experimental_manifest(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release, quality_status="experimental")
    settings = _settings(
        release,
        manifest_path,
        environment="production",
        release_quality_status="production-approved",
    )

    with pytest.raises(ValueError, match="production-approved"):
        validate_release(settings)


def test_valid_release_returns_stable_model_identities(tmp_path: Path) -> None:
    release = tmp_path / "release"
    manifest_path = _write_release(release)

    validated = validate_release(_settings(release, manifest_path))

    assert validated.release_id == "release-1"
    assert validated.models[TOP_MODEL].identity.name == TOP_MODEL
    assert validated.models[TOP_MODEL].identity.release_id == "release-1"
    assert validated.models[END_MODEL].labels == END_LABELS
