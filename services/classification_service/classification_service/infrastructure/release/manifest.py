import json
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from classification_service.domain.errors import DomainValidationError
from classification_service.domain.label_path import TopTriplePath
from classification_service.domain.model_identity import (
    END_DOC_CLASSIFIER_NAME,
    TOP_TRIPLE_CLASSIFIER_NAME,
)

NonEmptyString = Annotated[str, Field(min_length=1)]
QualityStatus = Literal["experimental", "production-approved"]


def validate_relative_posix_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or value != path.as_posix()
        or value.splitlines(keepends=True) != [value]
        or any(unicodedata.category(character) == "Cc" for character in value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("path must use canonical POSIX form and remain relative")
    return value


class StrictManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class ReleaseSource(StrictManifestModel):
    project: NonEmptyString
    training_run_id: NonEmptyString = Field(alias="trainingRunId")


class RuntimeVersions(StrictManifestModel):
    python: NonEmptyString
    setfit: NonEmptyString
    sentence_transformers: NonEmptyString = Field(alias="sentenceTransformers")
    transformers: NonEmptyString
    torch: NonEmptyString
    scikit_learn: NonEmptyString = Field(alias="scikitLearn")


class ChunkingConfig(StrictManifestModel):
    max_length: Literal[256] = Field(alias="maxLength")
    overlap: Literal[32]
    max_chunks_per_document: Literal[16] = Field(alias="maxChunksPerDocument")
    selection: Literal["uniform"]


class TokenizerManifest(StrictManifestModel):
    identity: NonEmptyString
    path: str

    @model_validator(mode="after")
    def validate_path(self) -> "TokenizerManifest":
        validate_relative_posix_path(self.path)
        if self.path != "tokenizer":
            raise ValueError("tokenizer path must be tokenizer")
        return self


class OfflineMetrics(StrictManifestModel):
    accuracy: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    macro_f1: float = Field(alias="macroF1", ge=0.0, le=1.0, allow_inf_nan=False)


class ModelManifest(StrictManifestModel):
    path: str
    labels: tuple[NonEmptyString, ...]
    label_count: int = Field(alias="labelCount", gt=0)
    metrics: OfflineMetrics

    @model_validator(mode="after")
    def validate_fields(self) -> "ModelManifest":
        validate_relative_posix_path(self.path)
        if self.label_count != len(self.labels):
            raise ValueError("labelCount must match labels length")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be unique")
        return self


class ReleaseManifest(StrictManifestModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    release_id: NonEmptyString = Field(alias="releaseId")
    quality_status: QualityStatus = Field(alias="qualityStatus")
    created_at: datetime = Field(alias="createdAt")
    source: ReleaseSource
    runtime_versions: RuntimeVersions = Field(alias="runtimeVersions")
    chunking: ChunkingConfig
    aggregation: Literal["arithmetic_mean"]
    tokenizer: TokenizerManifest
    models: dict[str, ModelManifest]

    @model_validator(mode="after")
    def validate_release_contract(self) -> "ReleaseManifest":
        if self.created_at.tzinfo is None:
            raise ValueError("createdAt must include a timezone")

        expected_names = {
            TOP_TRIPLE_CLASSIFIER_NAME,
            END_DOC_CLASSIFIER_NAME,
        }
        if set(self.models) != expected_names:
            raise ValueError(
                "models keys must contain exactly the supported classifiers"
            )

        top_model = self.models[TOP_TRIPLE_CLASSIFIER_NAME]
        end_model = self.models[END_DOC_CLASSIFIER_NAME]
        if top_model.path != f"{TOP_TRIPLE_CLASSIFIER_NAME}/model":
            raise ValueError("top classifier path does not match the release layout")
        if end_model.path != f"{END_DOC_CLASSIFIER_NAME}/model":
            raise ValueError(
                "end document classifier path does not match the release layout"
            )
        if len(top_model.labels) != 18:
            raise ValueError("top classifier labels must contain exactly 18 values")
        if len(end_model.labels) != 6:
            raise ValueError(
                "end document classifier labels must contain exactly 6 values"
            )

        for label in top_model.labels:
            try:
                TopTriplePath.from_leaf_label(label)
            except DomainValidationError as error:
                raise ValueError(
                    "top classifier labels must be complete three-level paths"
                ) from error
        return self

    @classmethod
    def load(cls, path: Path) -> "ReleaseManifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)
