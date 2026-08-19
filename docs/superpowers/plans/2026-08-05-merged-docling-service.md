# 合并 Docling 服务容器实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Docling HTTP API 与 RQ Worker 合并到一个受 Python PID 1 监管的容器，并让 Docling RQ 复用 TextProcessor Redis DB 1，同时保持 Celery 使用 DB 0。

**Architecture:** 基于固定 digest 的 Docling Serve 官方镜像构建轻量包装镜像，Python 监管器启动并监视 API 与 RQ Worker，联合健康检查验证两个子进程、HTTP API 和 Redis DB 1。Compose 只保留 `docling-api`，删除专用 Docling Redis 和独立 Worker；发布、验证和运行手册统一加载 `compose.yml + compose.docling.yml`。

**Tech Stack:** Docker/BuildKit、Docker Compose、Python 标准库、Docling Serve v1.28.0、RQ、Redis 7、PowerShell、pytest、GitHub Actions。

## Global Constraints

- Docling 继续使用 RQ，不迁移到 Celery。
- Docling API 与 RQ Worker 必须运行在同一个容器内，由 Python PID 1 监管。
- TextProcessor Celery 固定使用 `redis://redis:6379/0`。
- Docling RQ 固定使用 `redis://redis:6379/1`。
- 共享 Redis 不增加密码、ACL 或新的认证机制，也不发布宿主机端口。
- 生产环境不发布 Docling `5001` 端口，本地 override 可以显式发布。
- 保留 Compose service name `docling-api` 和内部 URL `http://docling-api:5001`。
- 任一 Docling 子进程异常退出时必须终止另一个子进程并让容器非零退出；监管器不在容器内无限重启。
- started RQ job 的恢复结果必须由真实故障测试确认，不得预先声称支持。
- 不修改结构化提取 API、processor 路由、格式 allowlist、模型或解析 profile。
- 只修改本计划列出的文件；保留现有未跟踪诊断文件，不执行 `git clean`、stash 或 reset。
- Git 提交说明使用中文，每个任务只提交该任务列出的文件。

---

## 文件结构

**新增：**

- `services/docling_service/process_manager.py`：PID 1 监管器，负责启动、状态发布、信号转发和整体退出。
- `services/docling_service/healthcheck.py`：联合检查子进程、Docling HTTP health 和 Redis DB 1。
- `services/docling_service/Dockerfile`：基于固定 Docling 基础镜像添加监管器和健康检查。
- `services/docling_service/tests/test_process_manager.py`：监管器生命周期和失败传播测试。
- `services/docling_service/tests/test_healthcheck.py`：联合健康检查单元测试。
- `services/docling_service/tests/test_container_contract.py`：Dockerfile、Compose、环境变量和网络静态契约。

**修改：**

- `compose.docling.yml`：将三个 Docling 服务收敛为一个 `docling-api`。
- `compose.override.yml`：删除失效的 `docling-worker/docling-redis` override。
- `.env.example`：区分 Docling 基础镜像与包装镜像，删除专用 Redis 密码。
- `scripts/verify-docling-deployment.ps1`：验证单容器双进程和共享 Redis DB 1。
- `scripts/verify-extraction-stack.ps1`：移除专用 Redis/Worker 服务检查，增加 DB 隔离检查。
- `docs/runbooks/structured-extraction.md`：更新启动、停止、恢复、升级和故障排查。
- `.github/workflows/deploy-staging.yml`：构建并启动合并 Docling 服务。
- `.github/workflows/deploy-production.yml`：按 release tag 构建并启动合并 Docling 服务。
- `deployment.md`：补充双 Compose 文件和 Docling 发布变量。

## Task 1: Python PID 1 监管器

**Files:**

- Create: `services/docling_service/process_manager.py`
- Create: `services/docling_service/tests/test_process_manager.py`

**Interfaces:**

- Produces: `ProcessSpec(name: str, argv: tuple[str, ...])`
- Produces: `run_supervisor(specs: tuple[ProcessSpec, ...], state_path: Path, grace_seconds: float, stop_event: threading.Event | None = None) -> int`
- Produces: state JSON with exactly `api` and `worker` keys whose values are positive integer PIDs, written atomically after both children start
- Production commands: `docling-serve run --host 0.0.0.0 --port 5001` and `docling-serve rq-worker`
- Consumed by: `services/docling_service/Dockerfile` and `healthcheck.py`

- [ ] **Step 1: 清理本轮早期测试产生的唯一缓存文件**

只删除以下已确认由本轮生成的文件和因此变空的目录，不触碰其他未跟踪文件：

```powershell
Remove-Item -LiteralPath "services/docling_service/tests/__pycache__/test_container_contract.cpython-314-pytest-9.1.1.pyc" -Force
```

Expected: `git status --short --untracked-files=all` 不再列出该 `.pyc`，仍保留 `.tmp-e2e-output/*` 和 `.tmp/diag_global_concurrency.py`。

- [ ] **Step 2: 写监管器失败测试**

在 `test_process_manager.py` 中覆盖以下可执行行为：

```python
def test_child_failure_terminates_sibling_and_returns_nonzero(tmp_path: Path) -> None:
    fast_failure = ProcessSpec(
        "api",
        (sys.executable, "-c", "raise SystemExit(23)"),
    )
    sleeping_worker = ProcessSpec(
        "worker",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )

    exit_code = run_supervisor(
        (fast_failure, sleeping_worker),
        tmp_path / "processes.json",
        grace_seconds=0.2,
    )

    assert exit_code != 0


def test_state_file_contains_both_child_pids(tmp_path: Path) -> None:
    state_path = tmp_path / "processes.json"
    stop_event = threading.Event()
    result: list[int] = []
    specs = (
        ProcessSpec("api", (sys.executable, "-c", "import time; time.sleep(60)")),
        ProcessSpec("worker", (sys.executable, "-c", "import time; time.sleep(60)")),
    )
    thread = threading.Thread(
        target=lambda: result.append(
            run_supervisor(specs, state_path, 0.2, stop_event)
        )
    )
    thread.start()
    wait_until_exists(state_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state) == {"api", "worker"}
    assert all(isinstance(pid, int) and pid > 0 for pid in state.values())
    stop_event.set()
    thread.join(timeout=5)
    assert result == [0]


def test_shutdown_forwards_termination_and_removes_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / "processes.json"
    stop_event = threading.Event()
    specs = (
        ProcessSpec("api", (sys.executable, "-c", "import time; time.sleep(60)")),
        ProcessSpec("worker", (sys.executable, "-c", "import time; time.sleep(60)")),
    )
    thread = threading.Thread(
        target=run_supervisor,
        args=(specs, state_path, 0.2, stop_event),
    )
    thread.start()
    wait_until_exists(state_path)
    child_pids = tuple(json.loads(state_path.read_text()).values())
    stop_event.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not state_path.exists()
    assert all(not process_is_alive(pid) for pid in child_pids)
```

实现测试辅助函数，不保留省略号；子进程脚本必须写入 `tmp_path`，不得依赖 Docling 安装。

- [ ] **Step 3: 运行测试并确认红灯**

Run:

```powershell
uv run --package app pytest services/docling_service/tests/test_process_manager.py -q
```

Expected: FAIL，原因是 `services.docling_service.process_manager` 尚不存在。

- [ ] **Step 4: 实现最小监管器**

`process_manager.py` 使用以下固定结构：

```python
@dataclass(frozen=True)
class ProcessSpec:
    name: str
    argv: tuple[str, ...]


def run_supervisor(
    specs: tuple[ProcessSpec, ...],
    state_path: Path,
    grace_seconds: float,
    stop_event: threading.Event | None = None,
) -> int:
    """Run all children; if one exits, terminate the rest and return non-zero."""


def main() -> int:
    specs = (
        ProcessSpec(
            "api",
            ("docling-serve", "run", "--host", "0.0.0.0", "--port", "5001"),
        ),
        ProcessSpec("worker", ("docling-serve", "rq-worker")),
    )
    return run_supervisor(
        specs,
        Path("/run/textprocessor-docling/processes.json"),
        grace_seconds=20.0,
    )
```

实现要求：

- 使用 `subprocess.Popen`，不使用 Shell；
- 通过临时文件加 `Path.replace()` 原子发布 state JSON；
- PID 1 捕获 `SIGTERM/SIGINT` 并转发；
- 使用有界轮询等待任一子进程退出；
- 正常停止时返回 0，意外子进程退出时返回非零；
- 在 `finally` 中终止残留进程并删除 state file；
- 只记录进程名称和退出码，不记录环境变量。

- [ ] **Step 5: 运行监管器测试和静态检查**

Run:

```powershell
uv run --package app pytest services/docling_service/tests/test_process_manager.py -q
uv run --package app ruff check services/docling_service/process_manager.py services/docling_service/tests/test_process_manager.py
```

Expected: tests PASS；Ruff exit 0。

- [ ] **Step 6: 提交监管器**

```powershell
git add services/docling_service/process_manager.py services/docling_service/tests/test_process_manager.py
git commit -m "实现：增加Docling双进程监管器"
```

## Task 2: 联合健康检查与包装镜像

**Files:**

- Create: `services/docling_service/healthcheck.py`
- Create: `services/docling_service/Dockerfile`
- Create: `services/docling_service/tests/test_healthcheck.py`
- Create: `services/docling_service/tests/test_container_contract.py`

**Interfaces:**

- Consumes: `/run/textprocessor-docling/processes.json` from Task 1
- Consumes: `DOCLING_SERVE_API_KEY`
- Consumes: `DOCLING_SERVE_ENG_RQ_REDIS_URL=redis://redis:6379/1`
- Produces: healthcheck process exit 0 only when both PIDs, HTTP health and Redis DB 1 are healthy
- Produces: image entrypoint `python /opt/textprocessor-docling/process_manager.py`

- [ ] **Step 1: 写健康检查单元测试**

通过依赖注入而非真实网络测试：

```python
def check_health(
    state_path: Path,
    api_key: str,
    redis_url: str,
    process_probe: Callable[[int], bool] = process_is_alive,
    http_probe: Callable[[str], bool] = api_is_healthy,
    redis_probe: Callable[[str], bool] = redis_is_healthy,
) -> bool:
    """Return true only when process, HTTP, and Redis probes all pass."""


def test_health_requires_api_and_worker_processes(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    state.write_text('{"api": 101, "worker": 102}', encoding="utf-8")
    assert check_health(
        state,
        "secret",
        "redis://redis:6379/1",
        process_probe=lambda pid: pid == 101,
        http_probe=lambda _key: True,
        redis_probe=lambda _url: True,
    ) is False


def test_health_requires_http_and_redis(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    state.write_text('{"api": 101, "worker": 102}', encoding="utf-8")
    assert check_health(
        state, "secret", "redis://redis:6379/1",
        process_probe=lambda _pid: True,
        http_probe=lambda _key: False,
        redis_probe=lambda _url: True,
    ) is False
    assert check_health(
        state, "secret", "redis://redis:6379/1",
        process_probe=lambda _pid: True,
        http_probe=lambda _key: True,
        redis_probe=lambda _url: False,
    ) is False


def test_health_passes_only_when_all_probes_pass(tmp_path: Path) -> None:
    state = tmp_path / "processes.json"
    state.write_text('{"api": 101, "worker": 102}', encoding="utf-8")
    assert check_health(
        state, "secret", "redis://redis:6379/1",
        process_probe=lambda _pid: True,
        http_probe=lambda _key: True,
        redis_probe=lambda url: url.endswith("/1"),
    ) is True
```

展开所有测试分支，不保留省略号。

- [ ] **Step 2: 写镜像静态契约测试**

`test_container_contract.py` 至少断言：

```python
def test_dockerfile_wraps_pinned_docling_image() -> None:
    content = (ROOT / "services/docling_service/Dockerfile").read_text(encoding="utf-8")
    assert "ARG DOCLING_BASE_IMAGE" in content
    assert "FROM ${DOCLING_BASE_IMAGE}" in content
    assert 'ENTRYPOINT ["python", "/opt/textprocessor-docling/process_manager.py"]' in content
    assert "HEALTHCHECK" not in content  # health timing stays in Compose


def test_runtime_assets_do_not_modify_docling_source() -> None:
    dockerfile = (ROOT / "services/docling_service/Dockerfile").read_text(encoding="utf-8")
    assert "process_manager.py" in dockerfile
    assert "healthcheck.py" in dockerfile
    assert "apt-get" not in dockerfile
    assert "pip install" not in dockerfile
```

- [ ] **Step 3: 运行测试并确认红灯**

```powershell
uv run --package app pytest services/docling_service/tests/test_healthcheck.py services/docling_service/tests/test_container_contract.py -q
```

Expected: FAIL，因为健康检查和 Dockerfile 尚不存在。

- [ ] **Step 4: 实现联合健康检查**

`healthcheck.py` 必须：

- 严格解析 state JSON，键必须包含 `api` 和 `worker`，PID 必须为正整数；
- 使用 `os.kill(pid, 0)` 检查进程；
- 使用 `urllib.request` 携带 `X-API-Key` 请求 `http://localhost:5001/health`；
- 使用 Docling 基础镜像已有的 `redis` Python package 对 Redis URL 执行 `PING`；
- 校验 URL 的 database 为 1，其他 DB 直接失败；
- 每个网络 probe 使用 5 秒超时；
- 任何异常都返回非零，不打印 secret 或完整 Redis URL。

入口：

```python
if __name__ == "__main__":
    raise SystemExit(0 if check_health_from_environment() else 1)
```

- [ ] **Step 5: 创建包装 Dockerfile**

```dockerfile
ARG DOCLING_BASE_IMAGE
FROM ${DOCLING_BASE_IMAGE}

USER root
RUN mkdir -p /opt/textprocessor-docling /run/textprocessor-docling
COPY --chmod=0555 services/docling_service/process_manager.py /opt/textprocessor-docling/process_manager.py
COPY --chmod=0555 services/docling_service/healthcheck.py /opt/textprocessor-docling/healthcheck.py

ENTRYPOINT ["python", "/opt/textprocessor-docling/process_manager.py"]
```

如果锁定基础镜像不是 root 默认用户，构建时回读其原用户并在创建目录后恢复该 UID；不得凭假设选择 UID。确保运行用户可以写 `/run/textprocessor-docling` 和模型缓存 volume。

- [ ] **Step 6: 运行单元测试和静态检查**

```powershell
uv run --package app pytest services/docling_service/tests -q
uv run --package app ruff check services/docling_service
```

Expected: tests PASS；Ruff exit 0。

- [ ] **Step 7: 构建镜像并回读入口**

```powershell
docker build `
  --build-arg "DOCLING_BASE_IMAGE=quay.io/docling-project/docling-serve-cpu:v1.28.0@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956" `
  -f services/docling_service/Dockerfile `
  -t textprocessor/docling-service:plan-smoke .
docker image inspect textprocessor/docling-service:plan-smoke --format '{{json .Config.Entrypoint}}'
```

Expected: build exit 0；Entrypoint 为监管器路径。

- [ ] **Step 8: 提交包装镜像**

```powershell
git add services/docling_service
git commit -m "构建：增加合并Docling服务镜像"
```

## Task 3: Compose 单容器与共享 Redis 契约

**Files:**

- Modify: `compose.docling.yml`
- Modify: `compose.override.yml`
- Modify: `.env.example`
- Modify: `services/docling_service/tests/test_container_contract.py`

**Interfaces:**

- Consumes: wrapper Dockerfile from Task 2
- Produces: Compose service `docling-api`
- Produces: image `${DOCKER_IMAGE_DOCLING-docling-service}:${TAG-latest}`
- Produces: build arg `${DOCLING_BASE_IMAGE?Variable not set}`
- Produces: RQ URL `redis://redis:6379/1`

- [ ] **Step 1: 扩展 Compose 红灯测试**

增加精确断言：

```python
def test_compose_has_one_docling_container_and_shared_redis() -> None:
    content = (ROOT / "compose.docling.yml").read_text(encoding="utf-8")
    assert "  docling-api:" in content
    assert "  docling-worker:" not in content
    assert "  docling-redis:" not in content
    assert "docling-redis-data" not in content
    assert "redis://redis:6379/1" in content
    assert "DOCLING_SERVE_ENG_KIND: rq" in content
    assert "services/docling_service/Dockerfile" in content


def test_celery_remains_on_redis_db_zero() -> None:
    content = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert content.count("CELERY_BROKER_URL=redis://redis:6379/0") == 3


def test_local_override_has_no_removed_docling_services() -> None:
    content = (ROOT / "compose.override.yml").read_text(encoding="utf-8")
    assert "  docling-worker:" not in content
    assert "  docling-redis:" not in content
```

- [ ] **Step 2: 运行测试并确认旧 Compose 失败**

```powershell
uv run --package app pytest services/docling_service/tests/test_container_contract.py -q
```

Expected: FAIL，显示旧 `docling-worker/docling-redis` 仍存在。

- [ ] **Step 3: 收敛 `compose.docling.yml`**

`docling-api` 必须包含：

```yaml
services:
  docling-api:
    image: '${DOCKER_IMAGE_DOCLING-docling-service}:${TAG-latest}'
    build:
      context: .
      dockerfile: services/docling_service/Dockerfile
      args:
        DOCLING_BASE_IMAGE: ${DOCLING_BASE_IMAGE?Variable not set}
    restart: always
    stop_grace_period: 30s
    depends_on:
      redis:
        condition: service_healthy
        restart: true
    environment:
      DOCLING_SERVE_API_KEY: ${DOCLING_SERVE_API_KEY?Variable not set}
      DOCLING_SERVE_ENG_KIND: rq
      DOCLING_SERVE_ENG_RQ_REDIS_URL: redis://redis:6379/1
      DOCLING_PROCESS_STATE_PATH: /run/textprocessor-docling/processes.json
      # 保留现有 UI、remote service、plugin、模型加载和输入限制配置。
    healthcheck:
      test: ["CMD", "python", "/opt/textprocessor-docling/healthcheck.py"]
      interval: 15s
      timeout: 10s
      retries: 20
      start_period: 120s
    volumes:
      - docling-model-cache:/root/.cache

volumes:
  docling-model-cache:
```

实际文件必须展开注释中列出的现有环境变量，不保留占位注释。

- [ ] **Step 4: 更新本地 override 和环境模板**

- `compose.override.yml` 保留 `docling-api` 的 `5001:5001`，删除另外两个 Docling service override；
- `.env.example` 将 `DOCLING_IMAGE` 重命名为 `DOCLING_BASE_IMAGE`；
- 新增 `DOCKER_IMAGE_DOCLING=docling-service`；
- 删除 `DOCLING_REDIS_PASSWORD`；
- 注释明确 Celery DB 0、Docling RQ DB 1 和共同故障域。

- [ ] **Step 5: 运行静态测试和 Compose 插值校验**

创建进程级临时环境变量，不写入 `.env`：

```powershell
$env:DOCLING_BASE_IMAGE = "quay.io/docling-project/docling-serve-cpu:v1.28.0@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956"
$env:DOCLING_SERVE_API_KEY = "compose-contract-only"
docker compose -f compose.yml -f compose.docling.yml config --quiet
uv run --package app pytest services/docling_service/tests/test_container_contract.py -q
```

Expected: Compose config exit 0；tests PASS。若主 Compose 其他必填变量缺失，只从当前受控 `.env` 读取，禁止把真实 secret 输出到日志。

- [ ] **Step 6: 提交 Compose 契约**

```powershell
git add compose.docling.yml compose.override.yml .env.example services/docling_service/tests/test_container_contract.py
git commit -m "部署：合并Docling容器并复用Redis"
```

## Task 4: 部署验证脚本与运行手册

**Files:**

- Modify: `scripts/verify-docling-deployment.ps1`
- Modify: `scripts/verify-extraction-stack.ps1`
- Modify: `docs/runbooks/structured-extraction.md`

**Interfaces:**

- Consumes: Compose service `docling-api` and shared `redis`
- Consumes: state file `/run/textprocessor-docling/processes.json`
- Produces: deployment verification that fails when either child is absent or Redis DBs are mixed

- [ ] **Step 1: 更新 Docling 服务集合检查**

将验证器的固定服务改为：

```powershell
$expectedServices = @("redis", "docling-api")
$removedServices = @("docling-redis", "docling-worker")
```

逐项断言 expected service 正在运行，并断言 removed service 不存在于 `docker compose config --services`。

- [ ] **Step 2: 增加容器内双进程与 DB 1 检查**

在现有 `$containerProbe` 中增加：

```python
import json
import os
from pathlib import Path

state = json.loads(
    Path("/run/textprocessor-docling/processes.json").read_text(encoding="utf-8")
)
if set(state) != {"api", "worker"}:
    raise SystemExit(1)
for pid in state.values():
    os.kill(int(pid), 0)
```

然后从共享 Redis 容器执行：

```powershell
Invoke-Compose exec -T redis redis-cli -n 1 PING | Select-String -SimpleMatch "PONG"
```

删除 API/Worker 镜像 ID 比较，因为不再有独立 Worker service。

- [ ] **Step 3: 更新完整栈验证器**

- 从 `$healthcheckedServices` 删除 `docling-redis`；
- 删除独立 `docling-worker` 状态阶段；
- 保留 `redis` 和 `docling-api` health 检查；
- 将阶段名改为 `Docling combined API/RQ service and shared Redis`；
- 所有 Celery `redis-cli` 操作显式增加 `-n 0`；
- Docling Redis 检查显式使用 `-n 1`；
- 增加检查：DB 0 的 smoke Celery queue 只在 DB 0 可见，在 DB 1 不可见。

- [ ] **Step 4: 更新运行手册**

文档必须明确：

- 启动服务改为 `redis docling-api`；
- API 与 RQ Worker 是同容器独立进程；
- DB 0/DB 1 的职责；
- 不再存在 `DOCLING_REDIS_PASSWORD` 和 `docling-redis-data`；
- 正常停止禁止 `down --volumes`，共享 `textprocessor-redis-data` 同时承载两套队列；
- queued/started 重启验收改为重启合并容器和共享 Redis；
- 故障排查通过 state file、容器日志、`redis-cli -n 1` 定位；
- logical DB 不提供资源或故障隔离；
- 升级时同时记录基础镜像 digest 和包装镜像 release tag。

- [ ] **Step 5: 执行 PowerShell 语法和静态引用检查**

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path scripts/verify-docling-deployment.ps1),
  [ref]$null,
  [ref]$errors
) | Out-Null
if ($errors.Count -ne 0) { $errors | Format-List; exit 1 }
[System.Management.Automation.Language.Parser]::ParseFile(
  (Resolve-Path scripts/verify-extraction-stack.ps1),
  [ref]$null,
  [ref]$errors
) | Out-Null
if ($errors.Count -ne 0) { $errors | Format-List; exit 1 }
rg -n "docling-redis|docling-worker|DOCLING_REDIS_PASSWORD" scripts docs/runbooks/structured-extraction.md
```

Expected: 两个脚本语法无错误；最后的 `rg` 无匹配并以 exit 1 结束，该 exit 1 表示旧引用已清除。

- [ ] **Step 6: 启动隔离栈并运行部署验证**

```powershell
$env:DOCLING_BASE_IMAGE = "quay.io/docling-project/docling-serve-cpu:v1.28.0@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956"
$env:DOCLING_SERVE_API_KEY = "replace-in-isolated-smoke"
docker compose -p textprocessor-docling-merged-smoke -f compose.yml -f compose.docling.yml up -d --build redis docling-api
pwsh -NoProfile -File scripts/verify-docling-deployment.ps1 -ComposeProjectName textprocessor-docling-merged-smoke
```

Expected: Redis 与 `docling-api` healthy；验证脚本输出 `Docling deployment verification passed.`。

- [ ] **Step 7: 提交验证与手册**

```powershell
git add scripts/verify-docling-deployment.ps1 scripts/verify-extraction-stack.ps1 docs/runbooks/structured-extraction.md
git commit -m "验证：覆盖合并Docling服务运行契约"
```

## Task 5: CI/CD 与发布文档

**Files:**

- Modify: `.github/workflows/deploy-staging.yml`
- Modify: `.github/workflows/deploy-production.yml`
- Modify: `deployment.md`

**Interfaces:**

- Consumes: `compose.yml` and `compose.docling.yml`
- Consumes: GitHub environment variable `DOCLING_BASE_IMAGE`
- Consumes: GitHub environment variable `DOCKER_IMAGE_DOCLING`
- Consumes: GitHub environment secret `DOCLING_SERVE_API_KEY`
- Produces: staging image tag `${{ github.sha }}`
- Produces: production image tag `${{ github.event.release.tag_name }}`

- [ ] **Step 1: 为部署工作流增加 Docling 环境和双 Compose 文件**

Staging job 增加：

```yaml
DOCLING_BASE_IMAGE: ${{ vars.DOCLING_BASE_IMAGE }}
DOCKER_IMAGE_DOCLING: ${{ vars.DOCKER_IMAGE_DOCLING }}
DOCLING_SERVE_API_KEY: ${{ secrets.DOCLING_SERVE_API_KEY }}
EXTRACTION_DOCLING_API_KEY: ${{ secrets.DOCLING_SERVE_API_KEY }}
TAG: ${{ github.sha }}
```

Production job 增加相同变量，但：

```yaml
TAG: ${{ github.event.release.tag_name }}
```

两个 workflow 的命令统一为：

```yaml
- run: docker compose -f compose.yml -f compose.docling.yml --project-name ${{ env.STACK_NAME }} build backend frontend classification-service docling-api
- run: docker compose -f compose.yml -f compose.docling.yml --project-name ${{ env.STACK_NAME }} up -d --wait --remove-orphans
```

保留现有环境 secrets，不在日志打印 Compose 展开配置。

- [ ] **Step 2: 更新 `deployment.md`**

明确记录：

- 必须创建上述两个 GitHub environment variables 和一个 secret；
- production/staging 都加载两份 Compose 文件；
- Docling 包装镜像与其他应用镜像使用相同 tag；
- 基础镜像使用固定 digest；
- Redis DB 0/1 映射与共同故障域；
- 生产服务器只开放 Traefik 80/443；
- 部署后运行 `verify-docling-deployment.ps1` 和完整栈 smoke。

- [ ] **Step 3: 验证 workflow 语法与危险模式**

```powershell
uv run zizmor .github/workflows/deploy-staging.yml .github/workflows/deploy-production.yml
rg -n "compose\.yml.*compose\.docling\.yml|DOCLING_BASE_IMAGE|DOCKER_IMAGE_DOCLING|DOCLING_SERVE_API_KEY|TAG:" .github/workflows/deploy-*.yml deployment.md
```

Expected: Zizmor 不新增 high severity finding；所有发布变量和双 Compose 命令均有匹配。

- [ ] **Step 4: 提交发布链路**

```powershell
git add .github/workflows/deploy-staging.yml .github/workflows/deploy-production.yml deployment.md
git commit -m "发布：将合并Docling服务纳入部署流程"
```

## Task 6: 完整回归、故障验证与交付审查

**Files:**

- Modify only if evidence requires correction: files changed in Tasks 1-5
- Verify: `docs/superpowers/specs/2026-08-05-merged-docling-service-design.md`
- Verify: `docs/superpowers/plans/2026-08-05-merged-docling-service.md`

**Interfaces:**

- Consumes: completed merged Docling stack
- Produces: verified release evidence or an explicit incomplete result

- [ ] **Step 1: 运行静态测试与代码质量门禁**

```powershell
uv run --package app pytest services/docling_service/tests -q
uv run --package app ruff check services/docling_service
docker compose -f compose.yml -f compose.docling.yml config --quiet
```

Expected: 全部 exit 0。

- [ ] **Step 2: 运行 Docling 部署验证**

```powershell
pwsh -NoProfile -File scripts/verify-docling-deployment.ps1 `
  -ComposeProjectName textprocessor-docling-merged-smoke `
  -ComposeFiles @("compose.yml", "compose.docling.yml")
```

Expected: 双进程、API 认证、OpenAPI、Redis DB 1 全部通过。

- [ ] **Step 3: 验证任一子进程退出导致整体重启**

分别读取 state file 中 `api` 与 `worker` PID，在隔离环境中依次发送 `SIGKILL`：

```powershell
docker compose -p textprocessor-docling-merged-smoke -f compose.yml -f compose.docling.yml exec -T docling-api python -c "import json,os; p=json.load(open('/run/textprocessor-docling/processes.json')); os.kill(p['api'], 9)"
docker compose -p textprocessor-docling-merged-smoke -f compose.yml -f compose.docling.yml ps docling-api
```

等待容器恢复 healthy 后，对 `worker` 重复同一过程。每次验证容器 restart count 增加，另一个旧子进程不存在，新 state file 包含两个新 PID。

- [ ] **Step 4: 验证 Redis logical DB 隔离**

运行 Task 4 已扩展的完整栈验证阶段。该阶段内部生成不可预测的唯一 Celery queue name，分别在 DB 0 和 DB 1 执行 `EXISTS`，并在 `finally` 中按 DB 删除该键；随后提交 Docling smoke job，仅比较 DB 1 前后的 RQ key 数量，不打印 key 内容或 job payload：

```powershell
pwsh -NoProfile -File scripts/verify-extraction-stack.ps1 `
  -ComposeProjectName textprocessor-docling-merged-smoke `
  -ComposeFiles @("compose.yml", "compose.docling.yml", "compose.override.yml")
```

Expected: `Celery broker message identity envelope` 和 `Redis DB isolation` 两个阶段均为 PASS。

- [ ] **Step 5: 运行真实异步转换和恢复矩阵**

使用获授权的脱敏 DOCX 样本：

```powershell
pwsh -NoProfile -File scripts/smoke-docling.ps1 `
  -BaseUrl "http://localhost:5001" `
  -ApiKey $env:DOCLING_SERVE_API_KEY `
  -SamplePath @($env:DOCLING_SMOKE_DOCX)
```

分别在 queued 和 started 状态重启 `docling-api` 容器，记录：task ID、最终状态、是否重复执行、是否需要人工恢复。started 无法恢复时将交付状态标为 `incomplete`，并按 Spec 在运行手册记录实际表现，不能将 healthcheck 通过替代恢复证据。

- [ ] **Step 6: 运行 TextProcessor 邻近回归**

```powershell
uv run --package app pytest backend/tests/features/structured_extraction backend/tests/integration/structured_extraction -q -m "not real_integration"
pwsh -NoProfile -File scripts/verify-extraction-stack.ps1 -ComposeProjectName textprocessor-docling-merged-smoke
```

Expected: 测试通过；完整栈验证确认 Celery DB 0 与 Docling DB 1 均正常。

- [ ] **Step 7: 审查最终差异和敏感信息**

```powershell
git diff --check
git diff --stat
git diff --name-only
git diff -- services/docling_service compose.docling.yml compose.override.yml .env.example scripts/verify-docling-deployment.ps1 scripts/verify-extraction-stack.ps1 docs/runbooks/structured-extraction.md .github/workflows/deploy-staging.yml .github/workflows/deploy-production.yml deployment.md
git status --short --untracked-files=all
```

确认：

- 没有真实 API key、Redis payload、样本路径或文档正文；
- 没有修改用户原有 `.tmp*` 文件；
- 没有残留 `docling-worker/docling-redis/DOCLING_REDIS_PASSWORD`；
- 所有真实未运行项明确报告为未验证。

- [ ] **Step 8: 提交最终验证修正**

只有 Step 1-7 发现并修正了实际问题时才创建此提交：

```powershell
git add services/docling_service compose.docling.yml compose.override.yml .env.example scripts/verify-docling-deployment.ps1 scripts/verify-extraction-stack.ps1 docs/runbooks/structured-extraction.md .github/workflows/deploy-staging.yml .github/workflows/deploy-production.yml deployment.md
git commit -m "修复：完善合并Docling服务验收"
```

若没有代码修正，不创建空提交。

## 交付判断

以下全部满足才可报告 `pass`：

- 单元、静态、Compose、部署验证和结构化提取回归均通过；
- 合并容器内 API 与 RQ Worker 均被真实检查；
- API/Worker 单进程故障都会触发容器整体恢复；
- Redis DB 0/1 隔离有真实键空间证据；
- 真实 Docling 异步转换通过；
- queued 与 started 重启恢复均有真实结果；
- 工作树差异仅包含授权范围且无敏感信息。

缺少真实样本、started 恢复证据、Docker/GPU/模型环境或 cleanup 证据时必须报告 `incomplete`，不能降级为通过。
