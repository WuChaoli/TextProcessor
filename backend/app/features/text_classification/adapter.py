import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class _Confidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topTriple: float = Field(ge=0, le=1)
    endDoc: float = Field(ge=0, le=1)


class _ClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: str
    requestId: str
    tags: tuple[str, str, str, str]
    confidence: _Confidence
    releaseId: str = Field(min_length=1, max_length=128)


class ClassificationClient:
    def __init__(self, *, base_url: str, api_token: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 60.0, max_response_bytes: int = 64 * 1024) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._transport = transport
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

    def classify(self, request_id: str, input_uri: str) -> dict[str, object]:
        headers = {"X-Request-ID": request_id}
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            response = client.post(
                f"{self._base_url}/internal/v1/classify",
                headers=headers,
                json={"schemaVersion": "1", "requestId": request_id, "inputUri": input_uri},
            )
        response.raise_for_status()
        if len(response.content) > self._max_response_bytes:
            raise ValueError("classification response is too large")
        try:
            payload = _ClassificationResponse.model_validate_json(response.content)
        except ValidationError:
            raise ValueError("classification response contract mismatch") from None
        if payload.requestId != request_id or payload.schemaVersion != "1":
            raise ValueError("classification response contract mismatch")
        return payload.model_dump(mode="json")
