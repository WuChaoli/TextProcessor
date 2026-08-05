from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def test_celery_runtime_stack_uses_durable_broker_and_health_checks() -> None:
    compose = (REPOSITORY_ROOT / "compose.yml").read_text(encoding="utf-8")

    assert "textprocessor-redis-data:/data" in compose
    assert "--appendonly" in compose
    assert "celery -A app.core.celery_app:celery_app inspect ping" in compose
    assert "condition: service_healthy" in compose
    assert "extraction-worker:" in compose
    assert "extraction-beat:" in compose
    assert compose.count("redis:\n        condition: service_healthy") >= 3


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


def test_stack_verifier_observes_real_broker_message_and_running_task_recovery() -> (
    None
):
    verifier = (REPOSITORY_ROOT / "scripts" / "verify-extraction-stack.ps1").read_text(
        encoding="utf-8"
    )

    assert "DEL celery" not in verifier
    assert "LINDEX celery 0" not in verifier
    assert "textprocessor-smoke-" in verifier
    assert "from celery import Celery" in verifier
    assert (
        "CeleryExtractionTaskDispatcher(smoke_app).enqueue_submit(task_id)" in verifier
    )
    assert "task_default_queue=queue_name" in verifier
    assert "submit_extraction_task.apply_async(" not in verifier
    assert "LINDEX $smokeQueue 0" in verifier
    assert "FromBase64String" in verifier
    assert "$headers.task" in verifier
    assert "$kwargs.task_id -ne $smokeTaskId" in verifier
    assert '$kwargs.task_type -ne "structured_extraction"' in verifier
    assert "$kwargs.schema_version -ne 1" in verifier
    assert "--queues" in verifier
    assert "LLEN $smokeQueue" in verifier
    assert "docker rm -f $smokeWorkerName" in verifier
    assert "redis-cli -n 0 DEL $smokeQueue" in verifier
    assert "redis-cli -n 1 DEL $smokeQueue" in verifier
    assert "redis-cli -n 0 --raw LINDEX $smokeQueue 0" in verifier
    assert "redis-cli -n 1 --raw LINDEX $smokeQueue 0" in verifier
    assert "task10-mineru" in verifier
    assert "--signal=KILL" in verifier
    assert "running:polling:task10-mineru" in verifier
    assert "succeeded::task10-mineru" in verifier
    assert "Get-ChildItem" in verifier
    assert "EXTRACTION_WORKER__PRODUCTION_FORMATS: '[\"pdf\"]'" in verifier
    assert "CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS: 5" in verifier
    assert "[Text.Encoding]::ASCII.GetBytes" in verifier
    assert '"up", "-d", "--force-recreate"' in verifier
    assert '"rm", "-sf", "task10-mineru"' in verifier
