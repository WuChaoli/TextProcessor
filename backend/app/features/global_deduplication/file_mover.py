import errno
import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MoveFailure:
    relative_path: str
    code: str

    def as_dict(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "code": self.code}


@dataclass(frozen=True, slots=True)
class MoveSummary:
    moved_duplicates: int
    failures: tuple[MoveFailure, ...]


def move_file(
    source: Path,
    destination_root: Path,
    *,
    relative_path: str,
    expected_sha256: str,
) -> MoveFailure | None:
    destination = destination_root / source.name
    try:
        if destination.exists():
            if sha256(destination) == expected_sha256 and not source.exists():
                return None
            return MoveFailure(relative_path, "OUTPUT_CONFLICT")
        try:
            os.link(source, destination)
        except FileExistsError:
            return MoveFailure(relative_path, "OUTPUT_CONFLICT")
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            _copy_then_remove(source, destination, expected_sha256)
        else:
            source.unlink()
        return None
    except FileExistsError:
        return MoveFailure(relative_path, "OUTPUT_CONFLICT")
    except OSError:
        return MoveFailure(relative_path, "MOVE_FAILED")


def _copy_then_remove(source: Path, destination: Path, digest: str) -> None:
    part = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    try:
        with source.open("rb") as input_file, part.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            output_file.flush()
            os.fsync(output_file.fileno())
        if sha256(part) != digest:
            raise OSError("temporary copy digest mismatch")
        os.link(part, destination)
        source.unlink()
    finally:
        part.unlink(missing_ok=True)
