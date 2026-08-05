from typing import Any

import httpx


class ClassificationClient:
    def __init__(self, *, base_url: str, api_token: str | None = None, transport: httpx.BaseTransport | None = None, timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token
        self._transport = transport
        self._timeout = timeout

    def classify(self, request_id: str, input_uri: str) -> dict[str, Any]:
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
        payload: dict[str, Any] = response.json()
        if payload.get("requestId") != request_id or payload.get("schemaVersion") != "1":
            raise ValueError("classification response contract mismatch")
        return payload
