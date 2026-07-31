import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from app.features.global_deduplication.errors import (
    GlobalDeduplicationErrorCode,
    GlobalDeduplicationProcessingError,
)
from app.features.global_deduplication.models import NormalizedDocument


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class GlobalDeduplicationStagingLayout:
    staging_root: Path
    task_id: uuid.UUID
    root: Path
    input_jsonl: Path
    mapping_json: Path
    datajuicer_result: Path
    final_result: Path
    manifest: Path

    @classmethod
    def for_task(cls, staging_root: Path, task_id: uuid.UUID) -> Self:
        normalized_root = staging_root.resolve(strict=False)
        task_root = normalized_root / str(task_id)
        return cls(
            staging_root=normalized_root,
            task_id=task_id,
            root=task_root,
            input_jsonl=task_root / "input.jsonl",
            mapping_json=task_root / "mapping.json",
            datajuicer_result=task_root / "datajuicer-result.jsonl",
            final_result=task_root / "final-result.json",
            manifest=task_root / "manifest.json",
        )

    def assert_safe(self) -> None:
        expected = (self.staging_root / str(self.task_id)).resolve(strict=False)
        if (
            self.root.resolve(strict=False) != expected
            or self.staging_root not in expected.parents
        ):
            raise ValueError("全局去重 staging 路径不安全")


@dataclass(frozen=True, slots=True)
class PreparedInput:
    layout: GlobalDeduplicationStagingLayout
    input_jsonl_sha256: str
    mapping_sha256: str
    input_document_count: int


class GlobalDeduplicationStaging:
    def __init__(self, staging_root: Path) -> None:
        self._staging_root = staging_root.resolve(strict=False)

    def prepare(
        self,
        task_id: uuid.UUID,
        documents: tuple[NormalizedDocument, ...],
        *,
        profile: str = "text_exact_minhash_v1",
    ) -> PreparedInput:
        if not documents:
            raise ValueError("staging 文档不能为空")
        layout = GlobalDeduplicationStagingLayout.for_task(
            self._staging_root,
            task_id,
        )
        layout.assert_safe()
        input_content = self._input_content(documents)
        mapping_content = self._mapping_content(task_id, documents)
        input_sha256 = _sha256(input_content)
        mapping_sha256 = _sha256(mapping_content)
        prepared = PreparedInput(
            layout=layout,
            input_jsonl_sha256=input_sha256,
            mapping_sha256=mapping_sha256,
            input_document_count=len(documents),
        )
        if self._can_reuse(prepared, input_content, mapping_content):
            return prepared
        if any(
            path.exists()
            for path in (layout.input_jsonl, layout.mapping_json, layout.manifest)
        ):
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.STAGING_INTEGRITY_FAILED,
                "已有 staging 与当前输入不一致",
            )
        try:
            layout.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._atomic_write(layout.input_jsonl, input_content)
            self._atomic_write(layout.mapping_json, mapping_content)
            now = datetime.now(UTC).isoformat()
            manifest_content = (
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "taskId": str(task_id),
                        "profile": profile,
                        "inputDocumentCount": len(documents),
                        "inputJsonlSha256": input_sha256,
                        "mappingSha256": mapping_sha256,
                        "datajuicerResultSha256": None,
                        "finalResultSha256": None,
                        "createdAt": now,
                        "updatedAt": now,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            self._atomic_write(layout.manifest, manifest_content)
        except GlobalDeduplicationProcessingError:
            raise
        except OSError:
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.STAGING_WRITE_FAILED,
                "无法写入任务 staging",
            ) from None
        return prepared

    def update_result_manifest(
        self,
        layout: GlobalDeduplicationStagingLayout,
        *,
        datajuicer_result_sha256: str,
        final_result_sha256: str,
    ) -> None:
        layout.assert_safe()
        try:
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("taskId") != str(layout.task_id)
            ):
                raise ValueError
            manifest["datajuicerResultSha256"] = datajuicer_result_sha256
            manifest["finalResultSha256"] = final_result_sha256
            manifest["updatedAt"] = datetime.now(UTC).isoformat()
            content = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            replacement = layout.manifest.with_name(".manifest.json.part")
            with replacement.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            replacement.replace(layout.manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            raise GlobalDeduplicationProcessingError(
                GlobalDeduplicationErrorCode.STAGING_WRITE_FAILED,
                "无法更新任务 manifest",
            ) from None
        finally:
            replacement = layout.manifest.with_name(".manifest.json.part")
            replacement.unlink(missing_ok=True)

    @staticmethod
    def _input_content(documents: tuple[NormalizedDocument, ...]) -> bytes:
        return "".join(
            json.dumps(
                {"uid": uid, "text": document.text},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
            for uid, document in enumerate(documents)
        ).encode()

    @staticmethod
    def _mapping_content(
        task_id: uuid.UUID,
        documents: tuple[NormalizedDocument, ...],
    ) -> bytes:
        return (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "taskId": str(task_id),
                    "documents": [
                        {
                            "uid": uid,
                            "fileId": document.reference.file_id,
                            "fileStoragePath": (
                                document.reference.file_storage_path
                            ),
                        }
                        for uid, document in enumerate(documents)
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        part = path.with_name(f".{path.name}.part")
        try:
            with part.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)

    @staticmethod
    def _can_reuse(
        prepared: PreparedInput,
        input_content: bytes,
        mapping_content: bytes,
    ) -> bool:
        layout = prepared.layout
        if not all(
            path.is_file()
            for path in (layout.input_jsonl, layout.mapping_json, layout.manifest)
        ):
            return False
        try:
            manifest = json.loads(layout.manifest.read_text(encoding="utf-8"))
            return (
                layout.input_jsonl.read_bytes() == input_content
                and layout.mapping_json.read_bytes() == mapping_content
                and manifest["taskId"] == str(layout.task_id)
                and manifest["inputDocumentCount"]
                == prepared.input_document_count
                and manifest["inputJsonlSha256"]
                == prepared.input_jsonl_sha256
                and manifest["mappingSha256"] == prepared.mapping_sha256
            )
        except (OSError, KeyError, json.JSONDecodeError, TypeError):
            return False
