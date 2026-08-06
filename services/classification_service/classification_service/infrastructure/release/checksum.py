import hashlib
import re
from pathlib import Path

from classification_service.infrastructure.release.manifest import (
    validate_relative_posix_path,
)

_CHECKSUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<path>.+)$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid checksum line {line_number}")
        relative_path = validate_relative_posix_path(match.group("path"))
        if relative_path == "checksums.sha256":
            raise ValueError("checksums.sha256 must not register itself")
        if relative_path in checksums:
            raise ValueError(f"duplicate checksum path: {relative_path}")
        checksums[relative_path] = match.group("digest")
    if not checksums:
        raise ValueError("checksums.sha256 must not be empty")
    return checksums


def write_checksums(root: Path, files: dict[str, Path]) -> Path:
    checksum_path = root / "checksums.sha256"
    lines = [f"{sha256_file(files[name])}  {name}" for name in sorted(files)]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksum_path
