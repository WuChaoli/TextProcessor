from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

_SNAKE_KEYS = frozenset({"task_id", "task_type", "schema_version"})
_CAMEL_KEYS = frozenset({"taskId", "taskType", "schemaVersion"})


@dataclass(frozen=True, slots=True)
class TaskEnvelope:
    task_id: UUID
    task_type: str
    schema_version: int

    def as_payload(self) -> dict[str, str | int]:
        return {
            "task_id": str(self.task_id),
            "task_type": self.task_type,
            "schema_version": self.schema_version,
        }

    @classmethod
    def parse(
        cls,
        payload: object,
        *,
        expected_type: str,
        expected_schema_version: int,
    ) -> TaskEnvelope:
        try:
            values = _normalized_payload(payload)
            task_id = UUID(values["task_id"])
            task_type = values["task_type"]
            schema_version = values["schema_version"]
            if task_type != expected_type:
                raise ValueError
            if isinstance(schema_version, bool) or not isinstance(schema_version, int):
                raise ValueError
            if schema_version != expected_schema_version:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("INVALID_TASK_ENVELOPE") from error
        return cls(task_id, task_type, schema_version)


def _normalized_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError
    values = cast(Mapping[str, object], payload)
    keys = frozenset(values.keys())
    if keys == _SNAKE_KEYS:
        return {
            "task_id": values["task_id"],
            "task_type": values["task_type"],
            "schema_version": values["schema_version"],
        }
    if keys == _CAMEL_KEYS:
        return {
            "task_id": values["taskId"],
            "task_type": values["taskType"],
            "schema_version": values["schemaVersion"],
        }
    raise ValueError
