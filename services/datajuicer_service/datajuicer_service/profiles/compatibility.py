import ast
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import data_juicer  # type: ignore[import-untyped]

from datajuicer_service.profiles.minhash import (
    MinHashConfig,
    _compute_signature,
    _optimal_parameters,
)

PINNED_VERSION = "1.5.4"
PINNED_COMMIT = "7061da6ad06287aa0305eda162429b34361a56a3"
REQUIRED_CONSTRUCTOR_FIELDS = {
    "tokenization",
    "window_size",
    "lowercase",
    "ignore_pattern",
    "num_permutations",
    "jaccard_threshold",
}


@dataclass(frozen=True, slots=True)
class DataJuicerRuntime:
    version: str
    commit: str
    import_path: str
    num_bands: int
    num_rows_per_band: int
    signature_bands: int
    signature_band_bytes: int
    signature_sha256: str


def _service_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_constructor_fields(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "DocumentMinhashDeduplicator":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    return {argument.arg for argument in item.args.args}
    raise RuntimeError("DATAJUICER_OPERATOR_NOT_FOUND")


def verify_datajuicer_runtime() -> DataJuicerRuntime:
    service_root = _service_root()
    source_root = (service_root / "vendor" / "data-juicer").resolve()
    import_path = Path(data_juicer.__file__).resolve()
    if data_juicer.__version__ != PINNED_VERSION:
        raise RuntimeError("DATAJUICER_VERSION_MISMATCH")
    if not import_path.is_relative_to(source_root):
        raise RuntimeError("DATAJUICER_NOT_LOADED_FROM_PINNED_SOURCE")

    result = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if commit != PINNED_COMMIT:
        raise RuntimeError("DATAJUICER_COMMIT_MISMATCH")

    operator_source = (
        source_root
        / "data_juicer"
        / "ops"
        / "deduplicator"
        / "document_minhash_deduplicator.py"
    )
    constructor_fields = _read_constructor_fields(operator_source)
    if not REQUIRED_CONSTRUCTOR_FIELDS.issubset(constructor_fields):
        raise RuntimeError("DATAJUICER_OPERATOR_CONTRACT_MISMATCH")

    config = MinHashConfig.v1()
    num_bands, rows_per_band = _optimal_parameters(
        config.jaccard_threshold,
        config.num_permutations,
    )
    signature = _compute_signature("Data-Juicer兼容性验证文本", config)
    if signature is None:
        raise RuntimeError("DATAJUICER_SIGNATURE_MISSING")
    return DataJuicerRuntime(
        version=data_juicer.__version__,
        commit=commit,
        import_path=str(import_path),
        num_bands=num_bands,
        num_rows_per_band=rows_per_band,
        signature_bands=len(signature),
        signature_band_bytes=len(signature[0]),
        signature_sha256=hashlib.sha256(b"".join(signature)).hexdigest(),
    )
