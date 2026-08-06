from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1"]
    requestId: str = Field(min_length=1, max_length=128)
    inputUri: str = Field(min_length=1, max_length=2048)


class ConfidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topTriple: float
    endDoc: float


class ClassifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1"] = "1"
    requestId: str
    tags: tuple[str, str, str, str]
    confidence: ConfidenceResponse
    releaseId: str


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schemaVersion: Literal["1"] = "1"
    requestId: str | None = None
    error: ErrorDetail
