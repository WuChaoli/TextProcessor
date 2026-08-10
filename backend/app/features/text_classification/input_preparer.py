import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from app.core.local_path_policy import (
    LocalPathAccessError,
    LocalPathAccessPolicy,
)


@dataclass(frozen=True, slots=True)
class PreparedClassificationInput:
    local_uri: str
    input_sha256: str
    size_bytes: int


class ClassificationInputPreparer:
    def __init__(
        self,
        *,
        staging_root: Path,
        max_input_bytes: int,
        local_paths: LocalPathAccessPolicy | None = None,
    ) -> None:
        self._staging_root = staging_root.resolve(strict=False)
        self._max_input_bytes = max_input_bytes
        self._local_paths = local_paths or LocalPathAccessPolicy()

    def prepare(self, task_id: str, input_uri: str) -> PreparedClassificationInput:
        parsed = urlsplit(input_uri)
        if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
            raise ValueError("input URI scheme is not allowed")
        raw_path = url2pathname(unquote(parsed.path)) if parsed.scheme else input_uri
        try:
            source = self._local_paths.preflight_input(raw_path)
        except LocalPathAccessError:
            raise ValueError("input path is not accessible") from None
        task_dir = (self._staging_root / task_id).resolve(strict=False)
        if not task_dir.is_relative_to(self._staging_root):
            raise ValueError("invalid task id")
        task_dir.mkdir(parents=True, exist_ok=True)
        destination = task_dir / "input.txt"
        destination_existed = destination.exists()
        digest = hashlib.sha256()
        size = 0
        try:
            with self._local_paths.open_regular_input(source) as input_file:
                if destination_existed:
                    while chunk := input_file.read(1024 * 1024):
                        size += len(chunk)
                        if size > self._max_input_bytes:
                            raise ValueError("input is too large")
                        digest.update(chunk)
                else:
                    with destination.open("xb") as output_file:
                        while chunk := input_file.read(1024 * 1024):
                            size += len(chunk)
                            if size > self._max_input_bytes:
                                raise ValueError("input is too large")
                            digest.update(chunk)
                            output_file.write(chunk)
        except LocalPathAccessError:
            raise ValueError("input path is not accessible") from None
        except Exception:
            if not destination_existed and destination.exists():
                destination.unlink(missing_ok=True)
            raise
        if destination_existed:
            existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            if (
                existing_digest != digest.hexdigest()
                or destination.stat().st_size != size
            ):
                raise ValueError("staged input conflicts with existing task input")
            return PreparedClassificationInput(
                destination.as_uri(),
                existing_digest,
                size,
            )
        return PreparedClassificationInput(destination.as_uri(), digest.hexdigest(), size)
