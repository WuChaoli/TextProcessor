# 本地路径访问能力策略实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除四项业务能力的本地路径 roots allowlist，改为由共享策略和 API/worker 实际运行账号权限决定本地文件访问能力。

**Architecture:** 新建与业务错误码解耦的 `LocalPathAccessPolicy`，API 使用它完成绝对路径、普通文件和输出父目录预检，worker 使用同一策略重新打开并通过 `fstat` 验证输入。各业务 adapter 将共享访问失败映射为自身稳定错误码；远程 URL/S3、内部 staging、格式/大小、冲突和原子发布策略保持独立。

**Tech Stack:** Python 3.14、FastAPI、SQLModel/PostgreSQL、Celery、fsspec、pytest、Ruff、Ty、Docker PostgreSQL。

## Global Constraints

- 不保留本地 roots 开关、空 roots 或 `/` roots 等兼容模式。
- API 预检不能替代 worker 的真实打开、`fstat`、大小检查和发布操作。
- 目录、设备、socket、FIFO 等非普通文件不得作为输入。
- 不创建缺失的调用方输出父目录，不覆盖已有目标，不削弱原子发布。
- 调用方路径不得控制 staging，也不得进入递归删除或权限修改操作。
- HTTP host/CIDR、S3 bucket、协议、凭据、格式、大小和超时限制保持不变。
- `INPUT_PATH_NOT_ALLOWED`、`OUTPUT_PATH_NOT_ALLOWED` 枚举保留一个兼容周期，但本地路径不再生成它们。
- 对外错误和默认日志不得泄露未经脱敏的宿主机绝对路径。

---

### Task 1: 共享本地路径访问策略

**Files:**
- Create: `backend/app/core/local_path_policy.py`
- Create: `backend/tests/core/test_local_path_policy.py`

**Interfaces:**
- Produces: `LocalPathAccessKind = Literal["input", "output"]`。
- Produces: `LocalPathAccessError(kind: LocalPathAccessKind, reason: str)`，`reason` 只使用受控分类，不包含原始路径。
- Produces: `LocalPathAccessPolicy.preflight_input(raw_path: str) -> Path`。
- Produces: `LocalPathAccessPolicy.preflight_output(raw_path: str, *, suffixes: frozenset[str]) -> Path`。
- Produces: `LocalPathAccessPolicy.open_regular_input(path: Path) -> ContextManager[BinaryIO]`，打开后用 `os.fstat()` 与 `stat.S_ISREG()` 验证。

- [ ] **Step 1: 写失败测试**

  覆盖 roots 之外普通文件、相对路径、缺失路径、目录、输出父目录缺失、父路径非目录、后缀限制和预检无残留文件。Linux 上使用 `os.mkfifo()` 验证 FIFO 被拒绝；符号链接覆盖有效、失效和循环情况。测试直接断言 `error.kind`、`error.reason` 等受控值，不断言原始绝对路径消息。

- [ ] **Step 2: 验证 RED**

  ```powershell
  uv run --project backend pytest backend/tests/core/test_local_path_policy.py -q
  ```

  预期因 `app.core.local_path_policy` 不存在而失败。

- [ ] **Step 3: 实现最小共享策略**

  `preflight_input()` 先验证绝对路径，再调用 `open_regular_input()`；`open_regular_input()` 使用 `path.open("rb")`，随后对文件描述符执行 `os.fstat()` 和 `stat.S_ISREG()`。`preflight_output()` 验证绝对路径、后缀、父目录存在且为目录，并用 `os.access(parent, os.W_OK)` 做快速预检，但不创建文件。所有 `OSError`、`RuntimeError` 统一转换为不包含路径的 `LocalPathAccessError`。

- [ ] **Step 4: 验证 GREEN 与静态检查**

  ```powershell
  uv run --project backend pytest backend/tests/core/test_local_path_policy.py -q
  uv run --project backend ruff check backend/app/core/local_path_policy.py backend/tests/core/test_local_path_policy.py
  uv run --project backend ty check backend/app/core/local_path_policy.py
  ```

- [ ] **Step 5: 提交**

  ```powershell
  git add backend/app/core/local_path_policy.py backend/tests/core/test_local_path_policy.py
  git commit -m "新增：统一本地路径访问能力检查"
  ```

### Task 2: 结构化提取接入共享策略

**Files:**
- Modify: `backend/app/features/structured_extraction/errors.py`
- Modify: `backend/app/features/structured_extraction/request_policy.py`
- Modify: `backend/app/features/structured_extraction/input_resolver.py`
- Modify: `backend/app/features/structured_extraction/processors/publisher.py`
- Modify: `backend/app/features/structured_extraction/orchestration.py`
- Modify: `backend/app/features/structured_extraction/routes.py`
- Modify: `backend/app/features/structured_extraction/celery_tasks.py`
- Test: `backend/tests/features/structured_extraction/test_request_policy.py`
- Test: `backend/tests/features/structured_extraction/test_input_resolver.py`
- Test: `backend/tests/features/structured_extraction/test_publisher.py`
- Test: `backend/tests/features/structured_extraction/test_orchestration.py`
- Test: `backend/tests/integration/structured_extraction/test_worker_pipeline.py`

**Interfaces:**
- Consumes: `LocalPathAccessPolicy` 和 `LocalPathAccessError`。
- Produces: `ExtractionErrorCode.INPUT_ACCESS_FAILED`、`ExtractionErrorCode.OUTPUT_ACCESS_FAILED`。
- Changes: `RequestPolicy.__init__()` 不再接收 `input_roots`、`output_roots`，改为可注入 `local_paths: LocalPathAccessPolicy | None = None`。
- Changes: `AtomicPublisher.__init__()` 不再接收 `output_roots`；目标必须是绝对 `.md`，父目录必须已存在，实际独占创建失败映射为输出访问错误或冲突。

- [ ] **Step 1: 写 API 和 worker 失败测试**

  将 request-policy 用例改为：roots 之外可读输入与既有可写父目录通过；缺失、目录、相对路径分别映射 `INPUT_ACCESS_FAILED` 或请求校验；缺失/不可写父目录映射 `OUTPUT_ACCESS_FAILED`。input resolver 用例验证 API 后删除或替换输入时 worker 返回 `INPUT_ACCESS_FAILED`。publisher 用例验证不创建父目录、无 roots 也能发布、目标冲突不变。

- [ ] **Step 2: 验证 RED**

  ```powershell
  uv run --project backend pytest backend/tests/features/structured_extraction/test_request_policy.py backend/tests/features/structured_extraction/test_input_resolver.py backend/tests/features/structured_extraction/test_publisher.py -q
  ```

  预期因构造参数和错误码仍采用 roots 模式而失败。

- [ ] **Step 3: 修改 API 策略和路由装配**

  request policy 的本地分支调用共享策略并映射稳定错误；HTTP/S3 代码保持原样。routes 创建策略时不再读取 settings roots。保留 `INPUT_PATH_NOT_ALLOWED`、`OUTPUT_PATH_NOT_ALLOWED` 枚举，但本地分支不得生成。

- [ ] **Step 4: 修改 worker 输入与发布**

  input resolver 对本地源使用 `open_regular_input()` 读取和复制，并以打开后的 `fstat().st_size` 进行上限判断。publisher 删除输出 roots 归属判断，不调用 `mkdir(parents=True)`；父目录缺失或独占临时文件创建失败映射 `OUTPUT_ACCESS_FAILED`，`FileExistsError` 仍进入摘要恢复/`OUTPUT_CONFLICT`。orchestration 和 celery task 删除 roots 参数传递。

- [ ] **Step 5: 验证结构化提取完整链路**

  ```powershell
  uv run --project backend pytest backend/tests/features/structured_extraction backend/tests/integration/structured_extraction -q
  uv run --project backend ruff check backend/app/features/structured_extraction backend/tests/features/structured_extraction backend/tests/integration/structured_extraction
  ```

- [ ] **Step 6: 提交**

  ```powershell
  git add backend/app/features/structured_extraction backend/tests/features/structured_extraction backend/tests/integration/structured_extraction
  git commit -m "修复：按运行账号权限访问结构化提取路径"
  ```

### Task 3: Markdown 清洗接入共享策略

**Files:**
- Modify: `backend/app/features/markdown_cleaning/api_errors.py`
- Modify: `backend/app/features/markdown_cleaning/errors.py`
- Modify: `backend/app/features/markdown_cleaning/request_policy.py`
- Modify: `backend/app/features/markdown_cleaning/input_resolver.py`
- Modify: `backend/app/features/markdown_cleaning/publisher.py`
- Modify: `backend/app/features/markdown_cleaning/orchestration.py`
- Modify: `backend/app/features/markdown_cleaning/routes.py`
- Modify: `backend/app/features/markdown_cleaning/celery_tasks.py`
- Test: `backend/tests/features/markdown_cleaning/test_request_policy.py`
- Test: `backend/tests/features/markdown_cleaning/test_input_resolver.py`
- Test: `backend/tests/features/markdown_cleaning/test_publisher.py`
- Test: `backend/tests/features/markdown_cleaning/test_orchestration.py`
- Test: `backend/tests/integration/markdown_cleaning/test_worker_pipeline.py`

**Interfaces:**
- Consumes: Task 1 的共享策略。
- Produces: API/worker 的 `INPUT_ACCESS_FAILED`、`OUTPUT_ACCESS_FAILED`。
- Changes: `MarkdownCleaningRequestPolicy` 和 publisher 删除 roots 构造参数；HTTP allowlist 接口保持不变。

- [ ] **Step 1: 写失败测试**

  将本地路径用例改为能力检查，新增 API 通过后 worker 输入消失、输出父目录变化、目标冲突和无残留探测文件用例。远程 URL host/CIDR 用例保持原断言，证明远程边界未放宽。

- [ ] **Step 2: 验证 RED**

  ```powershell
  uv run --project backend pytest backend/tests/features/markdown_cleaning/test_request_policy.py backend/tests/features/markdown_cleaning/test_input_resolver.py backend/tests/features/markdown_cleaning/test_publisher.py -q
  ```

- [ ] **Step 3: 实现 API 与 worker 接入**

  request policy 复用共享预检；resolver 通过共享打开接口完成真实读取；publisher 保留既有 staging 防护和原子发布，仅移除调用方目标 roots 判断及自动父目录创建。routes、orchestration、celery task 删除 roots 装配。

- [ ] **Step 4: 验证并提交**

  ```powershell
  uv run --project backend pytest backend/tests/features/markdown_cleaning backend/tests/integration/markdown_cleaning -q
  uv run --project backend ruff check backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning backend/tests/integration/markdown_cleaning
  git add backend/app/features/markdown_cleaning backend/tests/features/markdown_cleaning backend/tests/integration/markdown_cleaning
  git commit -m "修复：按运行账号权限访问 Markdown 清洗路径"
  ```

### Task 4: 全局去重接入共享策略

**Files:**
- Modify: `backend/app/features/global_deduplication/api_errors.py`
- Modify: `backend/app/features/global_deduplication/errors.py`
- Modify: `backend/app/features/global_deduplication/request_policy.py`
- Modify: `backend/app/features/global_deduplication/input_reader.py`
- Modify: `backend/app/features/global_deduplication/publisher.py`
- Modify: `backend/app/features/global_deduplication/orchestration.py`
- Modify: `backend/app/features/global_deduplication/routes.py`
- Modify: `backend/app/features/global_deduplication/celery_tasks.py`
- Test: `backend/tests/features/global_deduplication/test_request_policy.py`
- Test: `backend/tests/features/global_deduplication/test_input_reader.py`
- Test: `backend/tests/features/global_deduplication/test_publisher.py`
- Test: `backend/tests/features/global_deduplication/test_submit_orchestration.py`
- Test: `backend/tests/integration/global_deduplication/test_global_deduplication_worker_pipeline.py`

**Interfaces:**
- Consumes: Task 1 的共享策略。
- Produces: 本地输入清单、清单内本地文档和本地输出的稳定访问失败映射。
- Changes: `BoundedLocalReader` 删除 `input_roots`，接收 `local_paths`；`BoundedUriReader` 只对本地分支使用共享策略，HTTP/S3 参数不变。

- [ ] **Step 1: 写失败测试**

  覆盖 roots 之外的输入 manifest、manifest 引用文档和 JSON 输出；覆盖普通文件类型、大小上限、API/worker TOCTOU、输出父目录缺失以及远程 HTTP/S3 allowlist 不变。

- [ ] **Step 2: 验证 RED**

  ```powershell
  uv run --project backend pytest backend/tests/features/global_deduplication/test_request_policy.py backend/tests/features/global_deduplication/test_input_reader.py backend/tests/features/global_deduplication/test_publisher.py -q
  ```

- [ ] **Step 3: 实现本地能力接入**

  request policy 的 file/裸路径分支复用共享预检；`BoundedLocalReader` 通过共享打开接口分块读取并保持每文件/批次大小限制；publisher 删除本地输出 roots，但保留 S3 bucket 与发布冲突逻辑。routes、orchestration 和 celery task 删除本地 roots 参数。

- [ ] **Step 4: 验证并提交**

  ```powershell
  uv run --project backend pytest backend/tests/features/global_deduplication backend/tests/integration/global_deduplication -q
  uv run --project backend ruff check backend/app/features/global_deduplication backend/tests/features/global_deduplication backend/tests/integration/global_deduplication
  git add backend/app/features/global_deduplication backend/tests/features/global_deduplication backend/tests/integration/global_deduplication
  git commit -m "修复：按运行账号权限访问全局去重路径"
  ```

### Task 5: 文本分类接入共享输入策略

**Files:**
- Modify: `backend/app/features/text_classification/input_preparer.py`
- Modify: `backend/app/features/text_classification/celery_tasks.py`
- Test: `backend/tests/features/text_classification/test_contracts.py`
- Test: `backend/tests/features/text_classification/test_celery_tasks.py`

**Interfaces:**
- Consumes: Task 1 的 `LocalPathAccessPolicy.open_regular_input()`。
- Changes: `ClassificationInputPreparer.__init__(staging_root: Path, max_input_bytes: int, local_paths: LocalPathAccessPolicy | None = None)`，删除 `input_roots`。
- Preserves: staging 路径只由 `staging_root` 和 task ID 推导，已存在 staged input 仍按摘要幂等恢复。

- [ ] **Step 1: 写失败测试并验证 RED**

  测试 roots 之外普通文件成功、目录/FIFO/缺失文件失败、打开后大小检查、staging 逃逸防护和已有输入摘要恢复。

  ```powershell
  uv run --project backend pytest backend/tests/features/text_classification/test_contracts.py backend/tests/features/text_classification/test_celery_tasks.py -q
  ```

- [ ] **Step 2: 实现共享策略接入**

  保留 `file://` 与裸绝对路径协议解析；解析后交给共享策略打开，复制和摘要在同一已验证文件描述符上完成。celery task 不再读取 `CLASSIFICATION_INPUT_ROOTS`。

- [ ] **Step 3: 验证并提交**

  ```powershell
  uv run --project backend pytest backend/tests/features/text_classification -q
  uv run --project backend ruff check backend/app/features/text_classification backend/tests/features/text_classification
  git add backend/app/features/text_classification backend/tests/features/text_classification
  git commit -m "修复：按运行账号权限读取文本分类输入"
  ```

### Task 6: 删除 roots 配置并同步生产契约

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `compose.yml`
- Modify: `scripts/verify-markdown-cleaning-stack.ps1`
- Modify: `backend/app/features/markdown_cleaning/dependencies.py`
- Modify: `backend/tests/integration/markdown_cleaning/conftest.py`
- Modify: `backend/tests/features/structured_extraction/test_worker_config.py`
- Modify: `backend/tests/features/markdown_cleaning/test_worker_config.py`
- Modify: `backend/tests/features/global_deduplication/test_config.py`
- Modify: `backend/tests/features/text_classification/test_contracts.py`
- Modify: `AGENTS.md`
- Modify: `docs/runbooks/structured-extraction-137-production.md`
- Modify: `docs/runbooks/markdown-cleaning.md`
- Modify: `docs/superpowers/specs/2026-07-30-structured-extraction-task-design.md`
- Modify: `docs/superpowers/specs/2026-08-03-markdown-cleaning-task-design.md`
- Modify: `docs/superpowers/specs/2026-08-03-markdown-cleaning-worker-design.md`
- Modify: `docs/superpowers/specs/2026-08-04-three-layer-ddd-text-classification-design.md`

**Interfaces:**
- Removes: 设计文档第 9 节列出的九个环境变量和 Settings 字段。
- Preserves: 所有 staging、HTTP、S3、大小、超时和处理器配置。

- [ ] **Step 1: 写配置失败测试**

  测试 Settings 不再暴露九个 roots 字段，worker settings 不再要求 output roots，staging 配置仍完成安全规范化。以构造 Settings 和启动 API/worker 装配为行为断言，不以 grep 源码作为唯一测试。

- [ ] **Step 2: 验证 RED**

  ```powershell
  uv run --project backend pytest backend/tests/features/structured_extraction/test_worker_config.py backend/tests/features/markdown_cleaning/test_worker_config.py backend/tests/features/global_deduplication/test_config.py backend/tests/features/text_classification/test_contracts.py -q
  ```

- [ ] **Step 3: 删除配置与重复校验**

  从 Settings 模型和 validator 删除九个 roots 字段及输入/输出重叠检查；从 Compose、本地栈脚本、Markdown cleaning dependencies 和 integration fixture 删除注入。不得删除 staging 与 output target 的运行时文件级安全检查。

- [ ] **Step 4: 更新项目规则与运行手册**

  `AGENTS.md` 改为运行账号能力模型；运行手册列明专用非 root 账号、ACL、systemd sandbox、正负向权限检查和旧环境变量清理。历史报告只标注版本边界，不重写历史事实。

- [ ] **Step 5: 完整验证**

  使用专用临时 PostgreSQL 运行迁移和测试：

  ```powershell
  uv run --project backend ruff check backend/app backend/tests
  uv run --project backend ty check backend/app
  uv run --project backend pytest backend/tests -q -m "not real_integration"
  git diff --check
  ```

  Linux/137 验收另行使用真实非 root systemd 账号覆盖可读、不可读、可写、不可写、缺失父目录、已有目标、敏感路径不可访问和 staging 清理；Windows 单元测试不能替代该门禁。

- [ ] **Step 6: 提交**

  ```powershell
  git add backend/app/core/config.py backend/app/features/markdown_cleaning/dependencies.py compose.yml scripts/verify-markdown-cleaning-stack.ps1 backend/tests AGENTS.md docs
  git commit -m "配置：取消本地业务路径 roots 白名单"
  ```

### Task 7: OpenAPI、Apifox 与 137 发布验收

**Files:**
- Modify: OpenAPI/Apifox resources through the repository's existing synchronization workflow
- Create: `docs/reports/2026-08-10-local-path-access-policy-137-acceptance.md`

**Interfaces:**
- Consumes: 代码、测试、运行手册和实际 137 systemd 配置。
- Produces: 中文错误说明、生产环境测试证据和可回滚配置快照引用；不记录凭据或完整业务正文。

- [ ] **Step 1: 同步 API 契约**

  从运行代码导出 FastAPI OpenAPI；在修改 Apifox 资源前执行对应 `apifox cli-schema get` 与 `validate`，同步 `INPUT_ACCESS_FAILED`、`OUTPUT_ACCESS_FAILED` 及历史错误说明，写入后回读验证。

- [ ] **Step 2: 生产发布前安全核查**

  记录 API、Task Runner/Celery unit、User/Group、`ProtectSystem`、`ProtectHome`、`ReadOnlyPaths`、`ReadWritePaths`、`InaccessiblePaths`、目录 ACL 和环境配置备份。若进程是 root 或能读取选定敏感路径，停止发布并报告，不通过扩大应用权限继续。

- [ ] **Step 3: 滚动发布与真实路径验收**

  清理生产旧 roots 变量，执行配置解析和迁移检查，滚动重启真实消费者。使用 `/data/shineData/hub/txt` 与另一受控目录执行正向任务；使用不可读输入、不可写输出、缺失父目录和已有目标执行负向任务；验证 HTTP/S3 allowlist 和 staging 清理。

- [ ] **Step 4: 写验收报告并提交**

  报告逐项记录 task ID、最终状态、稳定错误码、运行账号权限结论、输出摘要、日志脱敏、远程策略和回滚点。不得记录 token、密码、绝对敏感路径内容或完整文档正文。

  ```powershell
  git add docs/reports/2026-08-10-local-path-access-policy-137-acceptance.md
  git commit -m "报告：记录 137 本地路径能力策略验收"
  ```
