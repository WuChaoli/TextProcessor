import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlmodel import Session
import psycopg

from app.core import config
from app.core.db import engine
from app.features.global_deduplication.request_policy import GlobalDeduplicationRequestPolicy
from app.features.global_deduplication.schemas import GlobalDeduplicationTaskCreate
from app.features.global_deduplication.service import GlobalDeduplicationTaskService
from app.features.global_deduplication.repository import GlobalDeduplicationTaskRepository
from app.models import User


class Dispatcher:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    def enqueue_submit(self, task_id: uuid.UUID) -> None:
        if self.fail:
            raise RuntimeError("broker unavailable")


def snapshot(task_id: uuid.UUID | None, note: str) -> None:
    with psycopg.connect(config.settings.SQLALCHEMY_DATABASE_URI) as conn:
        pid = conn.execute("select pg_backend_pid()").fetchone()[0]
        if task_id is None:
            print(f"{note} pid={pid} no_task_id")
            return
        rows = conn.execute(
            "select id::text,status,caller_id::text,session_id,input_json_path,target_path from global_deduplication_task where id=%s",
            (str(task_id),),
        ).fetchall()
        print(f"{note} pid={pid} rows={rows}")


def setup_caller() -> uuid.UUID:
    caller_id = uuid.uuid7()
    with Session(engine) as session:
        session.add(
            User(
                id=caller_id,
                email=f"diag-shared-{caller_id}@example.com",
                hashed_password="x",
            )
        )
        session.commit()
    return caller_id


def run_once(caller_id: uuid.UUID, session_id: str, fail: bool, path_tag: str) -> tuple[uuid.UUID | None, str]:
    root = Path(r"C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.tmp-e2e-output") / path_tag
    root.mkdir(parents=True, exist_ok=True)
    input_root = root / "in"
    output_root = root / "out"
    input_root.mkdir(exist_ok=True)
    output_root.mkdir(exist_ok=True)
    manifest = input_root / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")

    with Session(engine) as session:
        request = GlobalDeduplicationTaskCreate(
            sessionId=session_id,
            inputJsonPath=str(manifest),
            targetPath=str(output_root / "result.json"),
        )
        service = GlobalDeduplicationTaskService(
            repository=GlobalDeduplicationTaskRepository(session),
            policy=GlobalDeduplicationRequestPolicy(
                input_roots=(input_root,),
                output_roots=(output_root,),
                allowed_http_hosts=(),
                allowed_http_cidrs=(),
            ),
            dispatcher=Dispatcher(fail=fail),
        )
        try:
            task = service.create_task(caller_id, request)
            snapshot(task.id, f"[{path_tag}] after create success")
            return task.id, str(task.status)
        except Exception as exc:
            snapshot(None, f"[{path_tag}] exception {type(exc).__name__}: {exc}")
            return None, f"{type(exc).__name__}:{exc}"


def cleanup(caller_id: uuid.UUID) -> None:
    with Session(engine) as session:
        session.exec(
            f"delete from global_deduplication_task where caller_id='{caller_id}'"
        )
        session.exec(f"delete from " "user" " where id='{caller_id}'")
        session.commit()


def main() -> None:
    with Session(engine) as session:
        caller = setup_caller()
    session_id = f"session-{uuid.uuid7()}"
    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_once, caller, session_id, True, "gd-diag-a")
        f2 = executor.submit(run_once, caller, session_id, True, "gd-diag-b")
        print(f1.result(timeout=30))
        print(f2.result(timeout=30))

    with psycopg.connect(config.settings.SQLALCHEMY_DATABASE_URI) as conn:
        rows = conn.execute(
            "select id::text,status,error_code from global_deduplication_task where caller_id=%s",
            (str(caller),),
        ).fetchall()
        print('final rows for caller', rows)
    cleanup(caller)


if __name__ == "__main__":
    main()
