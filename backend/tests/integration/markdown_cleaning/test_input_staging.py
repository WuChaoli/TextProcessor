from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from app.features.markdown_cleaning.input_resolver import InputResolver
from app.features.markdown_cleaning.input_validator import MarkdownInputValidator
from app.features.markdown_cleaning.staging import StagingLayout


@dataclass
class ResolverTask:
    id: uuid.UUID
    file_storage_path: str | None
    file_oss_url: str | None
    selected_input_type: str
    input_sha256: str | None = None
    input_size_bytes: int | None = None


def test_resolver_and_validator_keep_bom_original_and_create_clean_source(
    markdown_cleaning_runtime,
) -> None:
    runtime = markdown_cleaning_runtime
    raw = b"\xef\xbb\xbf# \xe4\xb8\xad\xe6\x96\x87\n"
    normalized = raw[3:]
    runtime.source.write_bytes(raw)
    task_id = uuid.uuid4()
    layout = StagingLayout.for_task(runtime.staging_root, task_id)
    task = ResolverTask(
        id=task_id,
        file_storage_path=str(runtime.source),
        file_oss_url=None,
        selected_input_type="local",
    )
    resolved = InputResolver(
        input_roots=(runtime.source.parent,),
        allowed_http_hosts=(),
        allowed_http_cidrs=(),
        max_input_bytes=1024,
    ).resolve(task, layout)
    validated = MarkdownInputValidator(max_input_bytes=1024).validate(resolved, layout)

    assert layout.original_source.read_bytes() == raw
    assert validated.original_sha256 == hashlib.sha256(raw).hexdigest()
    assert layout.processor_source.read_bytes() == normalized
    assert validated.processor_sha256 == hashlib.sha256(normalized).hexdigest()
    assert layout.original_source != layout.processor_source

    layout.cleanup()
    assert not layout.root.exists()
