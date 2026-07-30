import hashlib
import json
from pathlib import Path
from typing import cast

import httpx

from app.core.config import MinerUProfile
from app.features.structured_extraction.errors import (
    ExtractionErrorCode,
    ExtractionProcessingError,
)
from app.features.structured_extraction.worker_models import (
    ExternalTaskState,
    ExternalTaskStatus,
    ExternalTaskSubmission,
    ProcessingContext,
    ProcessorArtifact,
    ProcessorName,
)

_PROCESSING_STATUSES = {"queued", "pending", "processing", "running"}
_MAX_SAFE_ERROR_LENGTH = 256


class MinerUHttpAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        profile: MinerUProfile,
        profile_name: str,
        api_key: str | None,
        client: httpx.Client,
        max_result_bytes: int,
    ) -> None:
        if max_result_bytes <= 0:
            raise ValueError("MinerU 最大结果大小必须为正数")
        self._base_url = base_url.rstrip("/")
        self._profile = profile
        self._profile_name = profile_name
        self._client = client
        self._max_result_bytes = max_result_bytes
        self._headers = {"X-API-Key": api_key} if api_key else {}
        profile_json = json.dumps(
            profile.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._profile_sha256 = hashlib.sha256(profile_json.encode()).hexdigest()

    def submit(
        self,
        source: Path,
        context: ProcessingContext,
    ) -> ExternalTaskSubmission:
        try:
            with source.open("rb") as input_file:
                response = self._client.post(
                    self._url("tasks"),
                    headers=self._headers,
                    files={
                        "files": (
                            source.name,
                            input_file,
                            "application/octet-stream",
                        )
                    },
                    data=self._profile_form(),
                )
        except OSError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.INPUT_ACCESS_FAILED,
                "无法读取 MinerU 提交文件",
            ) from None
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
                "MinerU 提交结果不确定",
            ) from None
        if response.status_code != 202:
            raise self._http_error(response, "MinerU 未接受任务")
        payload = self._json_object(response)
        external_task_id = payload.get("task_id")
        if not isinstance(external_task_id, str) or not external_task_id:
            raise invalid_response()
        return ExternalTaskSubmission(
            external_task_id=external_task_id,
            processor_name=ProcessorName.MINERU,
            processor_version=None,
        )

    def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        try:
            response = self._client.get(
                self._url("tasks", external_task_id),
                headers=self._headers,
            )
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "MinerU 状态查询暂时失败",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        if response.status_code != 200:
            raise self._http_error(
                response,
                "MinerU 状态查询失败",
                external_task_id=external_task_id,
            )
        payload = self._json_object(response)
        status = payload.get("status")
        if status in _PROCESSING_STATUSES:
            return ExternalTaskStatus(ExternalTaskState.PROCESSING)
        if status == "completed":
            return ExternalTaskStatus(ExternalTaskState.SUCCEEDED)
        if status == "failed":
            details = payload.get(
                "error",
                payload.get("message", "MinerU 处理失败"),
            )
            return ExternalTaskStatus(
                ExternalTaskState.FAILED,
                safe_error_code=ExtractionErrorCode.PROCESSING_FAILED,
                safe_error_message=self._safe_message(details),
            )
        raise invalid_response(external_task_id)

    def fetch_result(
        self,
        external_task_id: str,
        destination: Path,
    ) -> ProcessorArtifact:
        try:
            request = self._client.build_request(
                "GET",
                self._url("tasks", external_task_id, "result"),
                headers=self._headers,
            )
            response = self._client.send(request, stream=True)
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "MinerU 结果获取暂时失败",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        try:
            if response.status_code != 200:
                raise self._http_error(
                    response,
                    "MinerU 结果获取失败",
                    external_task_id=external_task_id,
                )
            payload = self._stream_json_object(
                response,
                limit=self._max_result_bytes,
            )
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "MinerU 结果获取暂时失败",
                transient=True,
                external_task_id=external_task_id,
            )
        finally:
            response.close()
        backend = payload.get("backend")
        version = payload.get("version")
        results = payload.get("results")
        if (
            not isinstance(backend, str)
            or not backend
            or not isinstance(version, str)
            or not version
            or not isinstance(results, dict)
            or len(results) != 1
        ):
            raise invalid_response(external_task_id)
        raw_result = next(iter(cast(dict[object, object], results).values()))
        if not isinstance(raw_result, dict):
            raise invalid_response(external_task_id)
        markdown = cast(dict[object, object], raw_result).get("md_content")
        if not isinstance(markdown, str) or not markdown.strip():
            raise invalid_response(external_task_id)
        encoded = markdown.encode("utf-8")
        if len(encoded) > self._max_result_bytes:
            raise invalid_response(external_task_id)
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(encoded)
        except OSError:
            destination.unlink(missing_ok=True)
            raise ExtractionProcessingError(
                ExtractionErrorCode.OUTPUT_WRITE_FAILED,
                "无法保存 MinerU 处理结果",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        return ProcessorArtifact(
            markdown_path=destination,
            processor_name=ProcessorName.MINERU,
            processor_version=version,
            profile_name=self._profile_name,
            profile_sha256=self._profile_sha256,
        )

    def _profile_form(self) -> dict[str, str]:
        fields: dict[str, str] = {}
        for name, value in self._profile.model_dump(mode="json").items():
            if isinstance(value, bool):
                fields[name] = str(value).lower()
            else:
                fields[name] = str(value)
        return fields

    def _json_object(
        self,
        response: httpx.Response,
        *,
        limit: int = 1024 * 1024,
    ) -> dict[str, object]:
        content_type = response.headers.get("content-type", "")
        if (
            "application/json" not in content_type.lower()
            or len(response.content) > limit
        ):
            raise invalid_response()
        try:
            payload = response.json()
        except ValueError, TypeError:
            raise invalid_response() from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise invalid_response()
        return cast(dict[str, object], payload)

    def _stream_json_object(
        self,
        response: httpx.Response,
        *,
        limit: int,
    ) -> dict[str, object]:
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise invalid_response()
        payload_bytes = bytearray()
        for chunk in response.iter_bytes():
            if len(payload_bytes) + len(chunk) > limit:
                raise invalid_response()
            payload_bytes.extend(chunk)
        try:
            payload = json.loads(payload_bytes)
        except ValueError, TypeError:
            raise invalid_response() from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise invalid_response()
        return cast(dict[str, object], payload)

    def _http_error(
        self,
        response: httpx.Response,
        fallback: str,
        *,
        external_task_id: str | None = None,
    ) -> ExtractionProcessingError:
        transient = response.status_code in {429, 502, 503, 504}
        return ExtractionProcessingError(
            ExtractionErrorCode.PROCESSING_FAILED,
            fallback,
            transient=transient,
            external_task_id=external_task_id,
        )

    @staticmethod
    def _safe_message(value: object) -> str:
        normalized = " ".join(str(value).split())
        return (normalized or "MinerU 处理失败")[:_MAX_SAFE_ERROR_LENGTH]

    def _url(self, *parts: str) -> str:
        suffix = "/".join(part.strip("/") for part in parts)
        return f"{self._base_url}/{suffix}"


def invalid_response(
    external_task_id: str | None = None,
) -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT,
        "MinerU 响应格式无效",
        external_task_id=external_task_id,
    )
