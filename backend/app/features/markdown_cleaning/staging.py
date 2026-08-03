from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or (
        hasattr(os.path, "isjunction") and os.path.isjunction(path)
    )


def _has_link_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if _is_link_or_junction(current):
            return True
    return False


@dataclass(frozen=True, slots=True)
class StagingLayout:
    """Task-owned staging paths derived exclusively from server configuration."""

    staging_root: Path
    task_id: uuid.UUID

    @classmethod
    def for_task(cls, staging_root: Path, task_id: uuid.UUID) -> StagingLayout:
        if not isinstance(task_id, uuid.UUID):
            raise TypeError("task_id 必须是 UUID")
        lexical_root = Path(os.path.abspath(staging_root))
        if _has_link_component(lexical_root):
            raise ValueError("staging 根目录不安全")
        return cls(staging_root=lexical_root.resolve(strict=False), task_id=task_id)

    @property
    def root(self) -> Path:
        return self.staging_root / str(self.task_id)

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def original_source(self) -> Path:
        return self.input_dir / "source.original.md"

    @property
    def processor_source(self) -> Path:
        return self.input_dir / "source.md"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def result(self) -> Path:
        return self.output_dir / "result.md"

    @property
    def publish_part(self) -> Path:
        return self.output_dir / "publish.md.part"

    def prepare(self) -> None:
        self._assert_safe()
        self.staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _is_link_or_junction(self.staging_root):
            raise ValueError("staging 根目录不安全")
        for directory in (self.root, self.input_dir, self.output_dir):
            if _is_link_or_junction(directory):
                raise ValueError("staging 任务目录不安全")
            directory.mkdir(mode=0o700, parents=False, exist_ok=True)
            if _is_link_or_junction(directory):
                raise ValueError("staging 任务目录不安全")
            try:
                directory.chmod(0o700)
            except OSError:
                # Windows ACLs do not implement POSIX modes; containment remains enforced.
                if os.name != "nt":
                    raise
        self._assert_safe()

    def cleanup(self) -> None:
        """Delete only the configured task directory, never a persisted DB path."""

        self._assert_safe()
        task_root = self.staging_root / str(self.task_id)
        if _is_link_or_junction(task_root):
            raise ValueError("staging 任务目录不安全")
        if not task_root.exists():
            return
        quarantine = self.staging_root / (f".cleanup-{self.task_id}-{uuid.uuid4().hex}")
        try:
            os.replace(task_root, quarantine)
        except FileNotFoundError:
            return
        if _is_link_or_junction(quarantine):
            self._unlink_reparse_entry(quarantine)
            raise ValueError("staging 任务目录在清理时被替换")
        self._remove_tree_no_follow(quarantine)

    @classmethod
    def _remove_tree_no_follow(cls, root: Path) -> None:
        if _is_link_or_junction(root):
            cls._unlink_reparse_entry(root)
            return
        with os.scandir(root) as entries:
            for entry in entries:
                child = Path(entry.path)
                if entry.is_symlink() or _is_link_or_junction(child):
                    cls._unlink_reparse_entry(child)
                elif entry.is_dir(follow_symlinks=False):
                    cls._remove_tree_no_follow(child)
                else:
                    child.unlink()
        root.rmdir()

    @staticmethod
    def _unlink_reparse_entry(path: Path) -> None:
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            os.rmdir(path)
        else:
            path.unlink()

    def assert_safe_path(self, path: Path, *, must_exist: bool = False) -> Path:
        self._assert_safe()
        lexical = Path(os.path.abspath(path))
        try:
            lexical.relative_to(self.root)
        except ValueError:
            raise ValueError("staging 路径不安全") from None
        if _has_link_component(lexical):
            raise ValueError("staging 路径不安全")
        try:
            resolved = lexical.resolve(strict=must_exist)
        except OSError:
            raise ValueError("staging 路径不安全") from None
        if not resolved.is_relative_to(self.root):
            raise ValueError("staging 路径不安全")
        return resolved

    def _assert_safe(self) -> None:
        expected_root = self.staging_root / str(self.task_id)
        if expected_root.parent != self.staging_root:
            raise ValueError("staging 任务目录不安全")
        if _has_link_component(self.staging_root):
            raise ValueError("staging 根目录不安全")
        if _is_link_or_junction(expected_root):
            raise ValueError("staging 任务目录不安全")
        if expected_root.exists():
            try:
                resolved = expected_root.resolve(strict=True)
            except OSError:
                raise ValueError("staging 任务目录不安全") from None
            if resolved != expected_root or not resolved.is_relative_to(
                self.staging_root
            ):
                raise ValueError("staging 任务目录不安全")
