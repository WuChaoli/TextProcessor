from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def test_celery_runtime_stack_uses_durable_broker_and_health_checks() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")

    assert "textprocessor-redis-data:/data" in compose
    assert "--appendonly" in compose
    assert "python" in compose and "app.task_runner.process_manager" in compose
    assert "condition: service_healthy" in compose
    assert "task-runner:" in compose
    assert "extraction-worker:" not in compose
    assert "extraction-beat:" not in compose
    assert compose.count("redis:\n        condition: service_healthy") >= 2


def test_celery_uses_configurable_redis_visibility_timeout() -> None:
    config = (REPOSITORY_ROOT / "backend" / "app" / "core" / "config.py").read_text(
        encoding="utf-8"
    )
    celery_app = (
        REPOSITORY_ROOT / "backend" / "app" / "core" / "celery_app.py"
    ).read_text(encoding="utf-8")

    assert "CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS" in config
    assert "broker_transport_options" in celery_app
    assert "visibility_timeout" in celery_app


def test_beat_uses_controlled_pid_and_schedule_paths() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    process_manager = (
        REPOSITORY_ROOT / "backend" / "app" / "task_runner" / "process_manager.py"
    ).read_text(encoding="utf-8")

    assert "--pidfile=/var/run/celery/beat.pid" in process_manager
    assert "--schedule=/var/lib/celery/beat-schedule" in process_manager
    assert "celery-beat-data:/var/lib/celery" in compose
    assert "mkdir -p /var/run/celery /var/run/textprocessor /var/lib/celery" in dockerfile


def test_docling_verifier_accepts_explicit_compose_project_and_files() -> None:
    verifier = (
        REPOSITORY_ROOT / "scripts" / "verify-docling-deployment.ps1"
    ).read_text(encoding="utf-8")

    assert "[string]$ComposeProjectName" in verifier
    assert "[string[]]$ComposeFiles" in verifier
    assert "@script:composeArguments" in verifier


def test_extraction_verifier_delegates_to_unified_single_node_verifier() -> None:
    verifier = (REPOSITORY_ROOT / "scripts" / "verify-extraction-stack.ps1").read_text(
        encoding="utf-8"
    )

    assert "verify-single-node-stack.ps1" in verifier
    assert "ComposeProjectName" in verifier
    assert "ComposeFiles" in verifier
    assert "SkipFaultInjection" in verifier
