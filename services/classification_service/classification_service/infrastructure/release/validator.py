import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from classification_service.domain.model_identity import ModelIdentity
from classification_service.infrastructure.config import Settings
from classification_service.infrastructure.release.checksum import (
    load_checksums,
    sha256_file,
)
from classification_service.infrastructure.release.manifest import (
    ModelManifest,
    ReleaseManifest,
)


@dataclass(frozen=True)
class ValidatedModel:
    identity: ModelIdentity
    path: Path
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedRelease:
    release_id: str
    root: Path
    tokenizer_path: Path
    models: Mapping[str, ValidatedModel]
    manifest: ReleaseManifest


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def _release_files(root: Path) -> dict[str, Path]:
    if not root.exists() or not root.is_dir():
        raise ValueError("model release must be an existing directory")
    if _is_link_like(root):
        raise ValueError("model release must not be a symbolic link or reparse point")

    resolved_root = root.resolve(strict=True)
    files: dict[str, Path] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if _is_link_like(path):
                    raise ValueError(
                        f"release contains a symbolic link or reparse point: {path.name}"
                    )
                try:
                    path.resolve(strict=True).relative_to(resolved_root)
                except ValueError as error:
                    raise ValueError("release path escapes release root") from error
                if entry.is_dir(follow_symlinks=False):
                    visit(path)
                elif entry.is_file(follow_symlinks=False):
                    relative_path = path.relative_to(root).as_posix()
                    files[relative_path] = path
                else:
                    raise ValueError(
                        f"release contains unsupported file type: {path.name}"
                    )

    visit(root)
    return files


def _required_directory(root: Path, relative_path: str, field_name: str) -> Path:
    path = root.joinpath(*relative_path.split("/"))
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise ValueError(
            f"{field_name} path is missing or escapes release root"
        ) from error
    if not path.is_dir():
        raise ValueError(f"{field_name} path must be a directory")
    return path


def _validated_model(
    root: Path, release_id: str, name: str, model: ModelManifest
) -> ValidatedModel:
    return ValidatedModel(
        identity=ModelIdentity(name=name, release_id=release_id),
        path=_required_directory(root, model.path, f"models.{name}"),
        labels=model.labels,
    )


def validate_release(settings: Settings) -> ValidatedRelease:
    root = settings.model_release
    files = _release_files(root)
    manifest_path = files.get("manifest.json")
    if manifest_path is None:
        raise ValueError("release is missing manifest.json")
    if sha256_file(manifest_path) != settings.model_release_sha256:
        raise ValueError("configured manifest SHA256 does not match manifest.json")

    manifest = ReleaseManifest.load(manifest_path)
    if (
        settings.environment == "production"
        and manifest.quality_status != "production-approved"
    ):
        raise ValueError("production requires a production-approved model release")
    if manifest.quality_status != settings.release_quality_status:
        raise ValueError("manifest quality status does not match configuration")

    checksum_path = files.get("checksums.sha256")
    if checksum_path is None:
        raise ValueError("release is missing checksums.sha256")
    registered = load_checksums(checksum_path)
    actual_names = set(files) - {"checksums.sha256"}
    registered_names = set(registered)
    missing = registered_names - actual_names
    if missing:
        raise ValueError(f"missing registered file: {min(missing)}")
    unregistered = actual_names - registered_names
    if unregistered:
        raise ValueError(f"unregistered file: {min(unregistered)}")
    for relative_path, expected in registered.items():
        if sha256_file(files[relative_path]) != expected:
            raise ValueError(f"checksum mismatch: {relative_path}")

    tokenizer_path = _required_directory(root, manifest.tokenizer.path, "tokenizer")
    models = MappingProxyType(
        {
            name: _validated_model(root, manifest.release_id, name, model)
            for name, model in manifest.models.items()
        }
    )
    return ValidatedRelease(
        release_id=manifest.release_id,
        root=root.resolve(strict=True),
        tokenizer_path=tokenizer_path,
        models=models,
        manifest=manifest,
    )
