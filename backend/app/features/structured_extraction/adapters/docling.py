import hashlib
import json
from pathlib import Path
from typing import cast

import httpx

from app.core.config import DoclingProfile
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

_PROCESSING_STATUSES = {"pending", "started"}
_MAX_SAFE_ERROR_LENGTH = 256


class DoclingHttpAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        profile: DoclingProfile,
        profile_name: str,
        api_key: str | None,
        client: httpx.Client,
        max_result_bytes: int,
    ) -> None:
        if max_result_bytes <= 0:
            raise ValueError("Docling 最大结果大小必须为正数")
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
        del context
        try:
            with source.open("rb") as input_file:
                response = self._client.post(
                    self._url("v1", "convert", "file", "async"),
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
                "无法读取 Docling 提交文件",
            ) from None
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSOR_SUBMISSION_UNCERTAIN,
                "Docling 提交结果不确定",
            ) from None
        if response.status_code != 200:
            raise self._http_error(response, "Docling 未接受任务")
        payload = self._json_object(response)
        external_task_id = payload.get("task_id")
        task_status = payload.get("task_status")
        if (
            not isinstance(external_task_id, str)
            or not external_task_id
            or task_status not in _PROCESSING_STATUSES | {"success", "failure"}
        ):
            raise invalid_response()
        return ExternalTaskSubmission(
            external_task_id=external_task_id,
            processor_name=ProcessorName.DOCLING,
            processor_version=None,
        )

    def get_status(self, external_task_id: str) -> ExternalTaskStatus:
        try:
            response = self._client.get(
                self._url("v1", "status", "poll", external_task_id),
                headers=self._headers,
            )
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "Docling 状态查询暂时失败",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        if response.status_code != 200:
            raise self._http_error(
                response,
                "Docling 状态查询失败",
                external_task_id=external_task_id,
            )
        payload = self._json_object(response, external_task_id=external_task_id)
        if payload.get("task_id") != external_task_id:
            raise invalid_response(external_task_id)
        status = payload.get("task_status")
        if status in _PROCESSING_STATUSES:
            return ExternalTaskStatus(ExternalTaskState.PROCESSING)
        if status == "success":
            return ExternalTaskStatus(ExternalTaskState.SUCCEEDED)
        if status == "failure":
            return ExternalTaskStatus(
                ExternalTaskState.FAILED,
                safe_error_code=ExtractionErrorCode.PROCESSING_FAILED,
                safe_error_message=self._failure_message(payload),
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
                self._url("v1", "result", external_task_id),
                headers=self._headers,
            )
            response = self._client.send(request, stream=True)
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "Docling 结果获取暂时失败",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        try:
            if response.status_code != 200:
                raise self._http_error(
                    response,
                    "Docling 结果获取失败",
                    external_task_id=external_task_id,
                )
            payload = self._stream_json_object(
                response,
                limit=self._max_result_bytes,
                external_task_id=external_task_id,
            )
        except httpx.RequestError:
            raise ExtractionProcessingError(
                ExtractionErrorCode.PROCESSING_FAILED,
                "Docling 结果获取暂时失败",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        finally:
            response.close()
        document = payload.get("document")
        if not isinstance(document, dict):
            raise invalid_response(external_task_id)
        markdown = cast(dict[object, object], document).get("md_content")
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
                "无法保存 Docling 处理结果",
                transient=True,
                external_task_id=external_task_id,
            ) from None
        return ProcessorArtifact(
            markdown_path=destination,
            processor_name=ProcessorName.DOCLING,
            processor_version=None,
            profile_name=self._profile_name,
            profile_sha256=self._profile_sha256,
        )

    def _profile_form(self) -> dict[str, str]:
        return {
            "to_formats": self._profile.to_formats[0],
            "image_export_mode": self._profile.image_export_mode,
            "ocr": str(self._profile.do_ocr).lower(),
            "table_mode": self._profile.table_mode,
        }

    def _json_object(
        self,
        response: httpx.Response,
        *,
        limit: int = 1024 * 1024,
        external_task_id: str | None = None,
    ) -> dict[str, object]:
        content_type = response.headers.get("content-type", "")
        if (
            "application/json" not in content_type.lower()
            or len(response.content) > limit
        ):
            raise invalid_response(external_task_id)
        try:
            payload = response.json()
        except ValueError, TypeError:
            raise invalid_response(external_task_id) from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise invalid_response(external_task_id)
        return cast(dict[str, object], payload)

    def _stream_json_object(
        self,
        response: httpx.Response,
        *,
        limit: int,
        external_task_id: str | None = None,
    ) -> dict[str, object]:
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise invalid_response(external_task_id)
        payload_bytes = bytearray()
        for chunk in response.iter_bytes():
            if len(payload_bytes) + len(chunk) > limit:
                raise invalid_response(external_task_id)
            payload_bytes.extend(chunk)
        try:
            payload = json.loads(payload_bytes)
        except ValueError, TypeError:
            raise invalid_response(external_task_id) from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise invalid_response(external_task_id)
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
    def _failure_message(payload: dict[str, object]) -> str:
        message = payload.get("error_message")
        if not isinstance(message, str):
            failure = payload.get("failure")
            if isinstance(failure, dict):
                message = cast(dict[object, object], failure).get("message")
        if not isinstance(message, str):
            message = "Docling 处理失败"
        normalized = " ".join(message.split())
        return (normalized or "Docling 处理失败")[:_MAX_SAFE_ERROR_LENGTH]

    def _url(self, *parts: str) -> str:
        suffix = "/".join(part.strip("/") for part in parts)
        return f"{self._base_url}/{suffix}"


def invalid_response(
    external_task_id: str | None = None,
) -> ExtractionProcessingError:
    return ExtractionProcessingError(
        ExtractionErrorCode.INVALID_PROCESSOR_OUTPUT,
        "Docling 响应格式无效",
        external_task_id=external_task_id,
    )
