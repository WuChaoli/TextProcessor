from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname  # ty: ignore[deprecated]


class InvalidInputUri(ValueError):
    pass


class InputTooLarge(InvalidInputUri):
    pass


class LocalTextReader:
    def __init__(self, root: Path, *, max_input_bytes: int) -> None:
        self._root = root.resolve(strict=True)
        self._max_input_bytes = max_input_bytes

    def read(self, input_uri: str) -> str:
        parsed = urlsplit(input_uri)
        if (
            parsed.scheme != "file"
            or parsed.netloc not in {"", "localhost"}
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidInputUri("input URI must be a local file URI")
        try:
            path = Path(url2pathname(unquote(parsed.path))).resolve(strict=True)  # ty: ignore[deprecated]
            path.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise InvalidInputUri("input URI is outside the staging root") from error
        if not path.is_file():
            raise InvalidInputUri("input URI must reference a regular file")
        size = path.stat().st_size
        if size > self._max_input_bytes:
            raise InputTooLarge("input exceeds the configured byte limit")
        try:
            return path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidInputUri("input must be UTF-8 text") from error
