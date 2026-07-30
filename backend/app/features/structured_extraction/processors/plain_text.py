import hashlib
import json
from pathlib import Path

from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.worker_models import (
    ProcessorArtifact,
    ProcessorName,
)


class PlainTextPassThroughProcessor:
    def __init__(
        self,
        encodings: tuple[str, ...] = ("utf-8-sig", "gb18030"),
        *,
        profile_name: str = "text-pass-through",
    ) -> None:
        if not encodings:
            raise ValueError("至少配置一种文本编码")
        self._encodings = encodings
        self._profile_name = profile_name
        profile = json.dumps(
            {"encodings": list(encodings)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._profile_sha256 = hashlib.sha256(profile.encode()).hexdigest()

    def process(self, source: Path, destination: Path) -> ProcessorArtifact:
        try:
            content = source.read_bytes()
        except OSError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INPUT_ACCESS_FAILED,
                "无法读取暂存输入",
            ) from None
        decoded: str | None = None
        for encoding in self._encodings:
            try:
                decoded = content.decode(encoding, errors="strict")
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "文本编码无法识别",
            )
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8", newline="") as output:
                output.write(decoded)
        except OSError:
            destination.unlink(missing_ok=True)
            raise ExtractionProcessingError(
                ExtractionErrorCode.OUTPUT_WRITE_FAILED,
                "无法写入结构化提取结果",
                transient=True,
            ) from None
        return ProcessorArtifact(
            markdown_path=destination,
            processor_name=ProcessorName.PLAIN_TEXT,
            processor_version="builtin",
            profile_name=self._profile_name,
            profile_sha256=self._profile_sha256,
        )
