from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DocumentReference:
    file_id: str
    file_storage_path: str


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    reference: DocumentReference
    text: str
    size_bytes: int
