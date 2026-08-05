import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


@dataclass(frozen=True, slots=True)
class PreparedClassificationInput:
    local_uri: str
    input_sha256: str
    size_bytes: int


class ClassificationInputPreparer:
    def __init__(self, *, staging_root: Path, input_roots: tuple[Path, ...], max_input_bytes: int) -> None:
        self._staging_root = staging_root.resolve(strict=False)
        self._input_roots = tuple(path.resolve(strict=False) for path in input_roots)
        self._max_input_bytes = max_input_bytes

    def prepare(self, task_id: str, input_uri: str) -> PreparedClassificationInput:
        parsed = urlsplit(input_uri)
        if parsed.scheme not in {"", "file"} or parsed.netloc not in {"", "localhost"}:
            raise ValueError("input URI scheme is not allowed")
        raw_path = url2pathname(unquote(parsed.path)) if parsed.scheme else input_uri
        source = Path(raw_path).resolve(strict=True)
        if not source.is_file() or not any(source.is_relative_to(root) for root in self._input_roots):
            raise ValueError("input path is not allowed")
        size = source.stat().st_size
        if size > self._max_input_bytes:
            raise ValueError("input is too large")
        task_dir = (self._staging_root / task_id).resolve(strict=False)
        if not task_dir.is_relative_to(self._staging_root):
            raise ValueError("invalid task id")
        task_dir.mkdir(parents=True, exist_ok=True)
        destination = task_dir / "input.txt"
        digest = hashlib.sha256()
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
                output_file.write(chunk)
        return PreparedClassificationInput(destination.as_uri(), digest.hexdigest(), size)
