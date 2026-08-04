import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from classification_service.infrastructure.config import (
    QualityStatus,
    RuntimeEnvironment,
    Settings,
)
from classification_service.infrastructure.release.checksum import sha256_file
from classification_service.infrastructure.release.manifest import ReleaseManifest
from classification_service.infrastructure.release.validator import validate_release


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a classification release without loading GPU models."
    )
    parser.add_argument("release", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument(
        "--environment",
        choices=("development", "staging", "production"),
        default="development",
    )
    parser.add_argument(
        "--quality-status",
        choices=("experimental", "production-approved"),
    )
    parser.add_argument("--manifest-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    release = arguments.release.resolve()
    manifest_path = release / "manifest.json"
    manifest = ReleaseManifest.load(manifest_path)
    quality_status = cast(
        QualityStatus, arguments.quality_status or manifest.quality_status
    )
    environment = cast(RuntimeEnvironment, arguments.environment)
    settings = Settings(
        environment=environment,
        internal_service_token=SecretStr("offline-validator"),
        model_root=(arguments.model_root or release.parent).resolve(),
        model_release=release,
        model_release_sha256=arguments.manifest_sha256 or sha256_file(manifest_path),
        release_quality_status=quality_status,
    )
    validated = validate_release(settings)
    sys.stdout.write(
        json.dumps(
            {
                "releaseId": validated.release_id,
                "qualityStatus": validated.manifest.quality_status,
                "models": sorted(validated.models),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
