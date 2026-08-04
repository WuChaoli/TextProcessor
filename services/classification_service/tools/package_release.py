import argparse
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from classification_service.domain.model_identity import CLASSIFIER_NAMES
from classification_service.infrastructure.release.checksum import write_checksums
from classification_service.infrastructure.release.manifest import ReleaseManifest
from classification_service.infrastructure.release.validator import _release_files


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def _copy_tree(source: Path, target: Path) -> None:
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"release source directory does not exist: {source}")
    if _is_link_like(source):
        raise ValueError("release source must not be a symbolic link or reparse point")
    target.mkdir(parents=True)
    with os.scandir(source) as entries:
        for entry in entries:
            source_path = Path(entry.path)
            if _is_link_like(source_path):
                raise ValueError(
                    "release source contains a symbolic link or reparse point"
                )
            target_path = target / entry.name
            if entry.is_dir(follow_symlinks=False):
                _copy_tree(source_path, target_path)
            elif entry.is_file(follow_symlinks=False):
                shutil.copyfile(source_path, target_path)
            else:
                raise ValueError("release source contains an unsupported file type")


def package_release(
    *,
    target: Path,
    tokenizer_source: Path,
    model_sources: Mapping[str, Path],
    manifest: ReleaseManifest,
) -> Path:
    if os.path.lexists(target):
        raise FileExistsError(f"release target already exists: {target}")
    if set(model_sources) != CLASSIFIER_NAMES:
        raise ValueError(
            "model_sources keys must contain exactly the supported classifiers"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    published = False
    try:
        _copy_tree(tokenizer_source, temporary / manifest.tokenizer.path)
        for name, model in manifest.models.items():
            _copy_tree(model_sources[name], temporary.joinpath(*model.path.split("/")))

        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            manifest.model_dump_json(by_alias=True, indent=2) + "\n",
            encoding="utf-8",
        )
        files = _release_files(temporary)
        write_checksums(temporary, files)
        temporary.rename(target)
        published = True
        return target
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Package a trusted immutable classification model release."
    )
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--tokenizer-source", type=Path, required=True)
    parser.add_argument("--top-model-source", type=Path, required=True)
    parser.add_argument("--end-model-source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manifest = ReleaseManifest.load(arguments.manifest)
    package_release(
        target=arguments.target,
        tokenizer_source=arguments.tokenizer_source,
        model_sources={
            "top-triple-classifier": arguments.top_model_source,
            "end-doc-classifier": arguments.end_model_source,
        },
        manifest=manifest,
    )
    sys.stdout.write(f"Packaged release at {arguments.target}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
