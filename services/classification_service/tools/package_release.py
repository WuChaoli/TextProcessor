import argparse
import ctypes
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from classification_service.domain.model_identity import CLASSIFIER_NAMES
from classification_service.infrastructure.release.checksum import write_checksums
from classification_service.infrastructure.release.manifest import (
    ReleaseManifest,
    validate_relative_posix_path,
)
from classification_service.infrastructure.release.validator import _release_files


def _is_link_like(path: Path) -> bool:
    metadata = path.lstat()
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return path.is_symlink() or bool(file_attributes & reparse_attribute)


def _validate_source_tree(source: Path) -> None:
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"release source directory does not exist: {source}")
    if _is_link_like(source):
        raise ValueError("release source must not be a symbolic link or reparse point")
    with os.scandir(source) as entries:
        for entry in entries:
            source_path = Path(entry.path)
            if _is_link_like(source_path):
                raise ValueError(
                    "release source contains a symbolic link or reparse point"
                )
            validate_relative_posix_path(source_path.relative_to(source).as_posix())
            if entry.is_dir(follow_symlinks=False):
                _validate_source_tree(source_path)
            elif not entry.is_file(follow_symlinks=False):
                raise ValueError("release source contains an unsupported file type")


def _copy_tree(source: Path, target: Path, *, source_root: Path | None = None) -> None:
    source_root = source if source_root is None else source_root
    target.mkdir(parents=True)
    with os.scandir(source) as entries:
        for entry in entries:
            source_path = Path(entry.path)
            if _is_link_like(source_path):
                raise ValueError(
                    "release source contains a symbolic link or reparse point"
                )
            relative_path = source_path.relative_to(source_root).as_posix()
            validate_relative_posix_path(relative_path)
            target_path = target / entry.name
            if entry.is_dir(follow_symlinks=False):
                _copy_tree(source_path, target_path, source_root=source_root)
            elif entry.is_file(follow_symlinks=False):
                shutil.copyfile(source_path, target_path)
            else:
                raise ValueError("release source contains an unsupported file type")


def atomic_publish_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a sibling directory without ever replacing target."""
    if source.parent.resolve() != target.parent.resolve():
        raise ValueError("atomic release publication requires sibling directories")

    if sys.platform == "win32":
        os.rename(source, target)
        return

    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2: Any = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise NotImplementedError(
                "atomic no-replace directory publication is unavailable"
            )
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(target),
            1,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), target)
        return

    raise NotImplementedError(
        "atomic no-replace directory publication is unavailable on this platform"
    )


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

    _validate_source_tree(tokenizer_source)
    for source in model_sources.values():
        _validate_source_tree(source)

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
        atomic_publish_directory_no_replace(temporary, target)
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
