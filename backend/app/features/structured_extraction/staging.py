import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StagingLayout:
    staging_root: Path
    task_id: uuid.UUID
    root: Path
    source: Path
    processor_dir: Path
    output: Path

    @classmethod
    def for_task(cls, staging_root: Path, task_id: uuid.UUID) -> StagingLayout:
        normalized_root = staging_root.resolve(strict=False)
        task_root = normalized_root / str(task_id)
        return cls(
            staging_root=normalized_root,
            task_id=task_id,
            root=task_root,
            source=task_root / "source" / "original",
            processor_dir=task_root / "processor",
            output=task_root / "output" / "result.md",
        )

    def prepare(self) -> None:
        self._assert_safe()
        for directory in (
            self.root,
            self.source.parent,
            self.processor_dir,
            self.output.parent,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def source_with_suffix(self, suffix: str) -> Path:
        normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        if normalized_suffix == "." or any(
            character in normalized_suffix for character in ("/", "\\", "\0")
        ):
            raise ValueError("无效的 staging 文件扩展名")
        return self.source.with_suffix(normalized_suffix.lower())

    def cleanup(self) -> None:
        self._assert_safe()
        if self.root.exists():
            shutil.rmtree(self.root)

    def _assert_safe(self) -> None:
        expected_root = (self.staging_root / str(self.task_id)).resolve(strict=False)
        actual_root = self.root.resolve(strict=False)
        if actual_root != expected_root or self.staging_root not in actual_root.parents:
            raise ValueError("staging 任务目录不安全")
