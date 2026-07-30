from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def test_celery_runtime_stack_uses_durable_broker_and_health_checks() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")

    assert "textprocessor-redis-data:/data" in compose
    assert "--appendonly" in compose
    assert "celery -A app.core.celery_app:celery_app inspect ping" in compose
    assert "condition: service_healthy" in compose


def test_beat_uses_controlled_pid_and_schedule_paths() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "--pidfile=/var/run/celery/beat.pid" in compose
    assert "--schedule=/var/lib/celery/beat-schedule" in compose
    assert "celery-beat-data:/var/lib/celery" in compose
    assert "mkdir -p /var/run/celery /var/lib/celery" in dockerfile


def test_docling_verifier_accepts_explicit_compose_project_and_files() -> None:
    verifier = (
        REPOSITORY_ROOT / "scripts" / "verify-docling-deployment.ps1"
    ).read_text(encoding="utf-8")

    assert "[string]$ComposeProjectName" in verifier
    assert "[string[]]$ComposeFiles" in verifier
    assert "@script:composeArguments" in verifier
