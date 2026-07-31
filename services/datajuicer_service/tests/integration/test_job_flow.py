import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from datajuicer_service.core.config import Settings, get_settings
from datajuicer_service.jobs.models import DataJuicerJob
from datajuicer_service.main import create_application
from datajuicer_service.worker import create_worker_application

pytestmark = pytest.mark.integration


def test_post_to_worker_to_get_flow_uses_real_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("DATAJUICER_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("DATAJUICER_TEST_DATABASE_URL is required")
    database_name = database_url.partition("?")[0].rsplit("/", maxsplit=1)[-1]
    if not database_name.endswith("_test"):
        pytest.fail("integration test requires a database ending in _test")

    repository_root = Path(__file__).resolve().parents[4]
    alembic_config = Config(
        repository_root / "services" / "datajuicer_service" / "alembic.ini"
    )
    monkeypatch.setenv("DATAJUICER_DATABASE_URL", database_url)
    monkeypatch.setenv("DATAJUICER_CELERY_BROKER_URL", "memory://")
    get_settings.cache_clear()
    command.upgrade(alembic_config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(delete(DataJuicerJob))

    input_path = tmp_path / "input.jsonl"
    output_path = tmp_path / "output.jsonl"
    full_text = "用于服务集成验证的真实中文段落，包含主体、数据与结论。" * 20
    input_path.write_text(
        "\n".join(
            [
                json.dumps({"uid": 0, "text": full_text}, ensure_ascii=False),
                json.dumps({"uid": 1, "text": f" {full_text}\n"}, ensure_ascii=False),
                json.dumps({"uid": 2, "text": full_text[:-1]}, ensure_ascii=False),
                json.dumps({"uid": 3, "text": "完全无关的数据库文档。" * 20}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    request = {
        "requestId": "source-integration-request-1",
        "profile": "text_exact_minhash_v1",
        "inputPath": str(input_path.resolve()),
        "outputPath": str(output_path.resolve()),
    }

    api_app = create_application()
    with TestClient(api_app) as client:
        accepted = client.post("/v1/jobs", json=request)
        assert accepted.status_code == 202
        job_id = UUID(accepted.json()["jobId"])

        with Session(engine) as session:
            assert session.scalar(
                select(func.count()).select_from(DataJuicerJob)
            ) == 1

        worker_app = create_worker_application(
            Settings(
                database_url=database_url,
                celery_broker_url="memory://",
            )
        )
        worker_app.conf.task_always_eager = True
        worker_app.tasks["datajuicer.execute"].apply(
            args=[
                {
                    "jobId": str(job_id),
                    "taskType": "datajuicer_job",
                    "schemaVersion": 1,
                }
            ]
        ).get()

        completed = client.get(f"/v1/jobs/{job_id}")
        assert completed.status_code == 200
        body = completed.json()
        assert body["status"] == "succeeded"
        output_bytes = output_path.read_bytes()
        assert body["result"]["outputSha256"] == hashlib.sha256(
            output_bytes
        ).hexdigest()
        records = [
            json.loads(line) for line in output_bytes.decode("utf-8").splitlines()
        ]
        assert {record["uid"] for record in records} == {0, 1, 2, 3}
        assert len(records) == 4

    with engine.begin() as connection:
        connection.execute(delete(DataJuicerJob))
    engine.dispose()
    get_settings.cache_clear()
