# Structured Extraction Production Formats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 137 保持 TextProcessor systemd 部署的前提下接入既有 MinerU、部署 Docling、开放八类输入，并用真实生产任务证明结果内容正确。

**Architecture:** TextProcessor API、Task Runner、PostgreSQL 和 Redis 保持现状；MinerU 作为外部 HTTP 依赖，Docling 作为 137 本机 Docker 服务。Worker 通过环境配置连接处理器，并以 `production_formats` 作为生产放行门；测试失败只报告，不自动收紧。

**Tech Stack:** FastAPI、Celery、PostgreSQL、Redis、Pydantic Settings、MinerU HTTP API、Docling Serve、Docker Compose、pytest、PowerShell、systemd。

## Global Constraints

- 先接通上线并开放格式，再执行逐格式测试；测试失败不自动收紧。
- 目标格式为 PDF、PNG、JPG、PPTX、XLSX、DOCX、HTML、EPUB；DOCX 同时覆盖 Docling 与 MinerU 路由。
- 不启用 DOC、PPT、WPS、ET、DPS、OFD。
- MinerU 是独立外部服务，不加入 TextProcessor Compose 或回滚操作。
- 不提交真实凭据、生产密码、完整业务正文或不必要的绝对路径。
- 不修改或提交用户已有的 `AGENTS.md` 和无关未跟踪文件。

---

### Task 1: 固化逐格式样本和测试契约

**Files:**
- Create: `backend/tests/fixtures/structured_extraction/synthetic-smoke.jpg`
- Create: `backend/tests/fixtures/structured_extraction/synthetic-smoke.html`
- Create: `backend/tests/fixtures/structured_extraction/synthetic-smoke.epub`
- Create: `backend/tests/fixtures/structured_extraction/synthetic-complex.docx`
- Modify: `backend/tests/integration/structured_extraction/test_mineru_real.py`
- Modify: `backend/tests/integration/structured_extraction/test_docling_real.py`
- Modify: `scripts/smoke-mineru.ps1`
- Modify: `scripts/smoke-docling.ps1`

**Interfaces:**
- Consumes: `MINERU_REAL_SAMPLE_PATHS` and `DOCLING_REAL_SAMPLE_PATHS` JSON objects.
- Produces: independent keys `pdf`, `png`, `jpg`, `pptx`, `docx`, `xlsx`, `html`, `epub` and per-format content expectations.

- [ ] **Step 1: Add failing smoke contract tests**

Update MinerU tests and script tests so PNG and JPG are distinct required keys. Add content expectation JSON parsing and assert each fetched Markdown contains every configured normalized phrase. Extend Docling tests with the same content expectation contract.

- [ ] **Step 2: Run focused tests and verify the old combined image contract fails**

Run:

```powershell
cd backend
uv run pytest tests/features/structured_extraction/test_deployment_stack.py tests/integration/structured_extraction/test_mineru_real.py tests/integration/structured_extraction/test_docling_real.py -q -m "not real_integration"
```

Expected: contract assertions fail until scripts and fixtures are updated.

- [ ] **Step 3: Create deterministic non-sensitive fixtures**

Generate the four synthetic files with stable phrases:

```text
JPG: TEXTPROCESSOR JPG SMOKE 20260807
HTML: TEXTPROCESSOR HTML SMOKE 20260807, table value HTML-CELL-42
EPUB: TEXTPROCESSOR EPUB SMOKE 20260807, chapter EPUB-CHAPTER-ONE
complex DOCX: TEXTPROCESSOR COMPLEX DOCX SMOKE 20260807
```

The complex DOCX must contain enough supported visual structures to make `OfficeDocumentInspector` choose MinerU under the configured threshold; verify this with a focused unit test before using it as a production fixture.

- [ ] **Step 4: Implement separate sample and expectation handling**

Require `pdf/png/jpg/pptx` in `smoke-mineru.ps1`; require `docx/xlsx/html/epub` in `smoke-docling.ps1`. Pass expectation objects through `MINERU_REAL_EXPECTATIONS` and `DOCLING_REAL_EXPECTATIONS`; never print full extracted Markdown.

- [ ] **Step 5: Run focused tests and syntax checks**

Run pytest for structured-extraction integration modules with `not real_integration`, parse both PowerShell scripts with the PowerShell parser, and run Ruff on both Python modules.

- [ ] **Step 6: Commit fixture and contract changes**

```powershell
git add backend/tests/fixtures/structured_extraction backend/tests/integration/structured_extraction/test_mineru_real.py backend/tests/integration/structured_extraction/test_docling_real.py scripts/smoke-mineru.ps1 scripts/smoke-docling.ps1
git commit -m "测试：补齐结构化提取逐格式真实验收"
```

### Task 2: 增加 137 混合部署运行手册与配置模板

**Files:**
- Modify: `.env.example`
- Modify: `docs/runbooks/structured-extraction.md`
- Create: `docs/runbooks/structured-extraction-137-production.md`

**Interfaces:**
- Consumes: existing systemd units and independent MinerU URL.
- Produces: exact non-secret environment variable names and safe deployment/rollback procedure.

- [ ] **Step 1: Add configuration examples**

Document nested settings names:

```dotenv
EXTRACTION_WORKER__MINERU_BASE_URL=http://127.0.0.1:9000
EXTRACTION_WORKER__MINERU_API_KEY=
EXTRACTION_WORKER__DOCLING_BASE_URL=http://127.0.0.1:5001
EXTRACTION_WORKER__DOCLING_API_KEY=replace-with-runtime-secret
EXTRACTION_WORKER__PRODUCTION_FORMATS=["text","markdown","json","xml","yaml","csv","tsv","pdf","image","pptx","xlsx","docx","html","epub"]
```

- [ ] **Step 2: Document mixed deployment operations**

Record baseline capture, exact unit discovery, environment backup, Docling container lifecycle, configuration parsing, selective restart, health checks, post-release observation, user-authorized-only format tightening, and non-destructive rollback.

- [ ] **Step 3: Verify documentation and secret boundaries**

Run `git diff --check`, search for real IP passwords/tokens and confirm only placeholders are tracked.

- [ ] **Step 4: Commit runbook changes**

```powershell
git add .env.example docs/runbooks/structured-extraction.md docs/runbooks/structured-extraction-137-production.md
git commit -m "文档：补充137结构化提取混合部署手册"
```

### Task 3: 核对并接通 137 的 MinerU 与 Docling

**Files:**
- Modify on server only: exact systemd environment file discovered from active units.
- Create on server only: controlled Docling deployment files or use the repository Compose definition without migrating other services.

**Interfaces:**
- Produces: reachable MinerU endpoint and authenticated Docling endpoint from the TextProcessor worker host.

- [ ] **Step 1: Capture production baseline**

Record active unit names, child PIDs, listening ports, current non-secret structured extraction settings, queue state, deployed source hash and exact environment-file paths. Do not print secrets.

- [ ] **Step 2: Verify existing MinerU**

Check health, OpenAPI/protocol endpoints and one minimal API-level connection operation. Do not restart or modify MinerU.

- [ ] **Step 3: Deploy only Docling dependencies**

Build or pull the repository-pinned Docling image, start Docling against an explicitly selected Redis endpoint/database, bind its API to localhost or an approved internal interface, and wait for healthy. Avoid starting or replacing TextProcessor API, database, Redis, classification or Data-Juicer services.

- [ ] **Step 4: Verify Docling runtime contract**

Run the repository Docling deployment verifier or equivalent checks for API, worker process, authentication and Redis DB. Record container/image identifiers and health evidence without credentials.

### Task 4: 开放生产格式并重启实际消费者

**Files:**
- Modify on server only: discovered TextProcessor runtime environment configuration.

**Interfaces:**
- Produces: running worker settings with the complete production allowlist and both processor endpoints.

- [ ] **Step 1: Back up exact runtime configuration**

Create a timestamped backup beside the exact environment file after resolving and validating its absolute path under the deployment root or `/etc` unit configuration. Record backup path without displaying secret content.

- [ ] **Step 2: Write processor settings and allowlist**

Set the MinerU URL, Docling URL/API key, timeout/profile settings required by the existing adapters, and the full JSON allowlist from the Spec.

- [ ] **Step 3: Validate settings before restart**

Run the deployed Python environment with the service environment and print only normalized non-secret fields: processor host/port, profile name and production formats. Any parse failure stops the restart.

- [ ] **Step 4: Restart only actual consumers**

Restart the Task Runner/Celery unit that loads `ExtractionWorkerSettings`; restart API only if process inspection proves it consumes changed values. Confirm active state, new PIDs and absence of startup errors.

- [ ] **Step 5: Confirm formats are open**

Submit or route a controlled target format far enough to prove the former allowlist rejection is gone. Do not interpret later processor failure as allowlist failure.

### Task 5: 执行处理器直连与生产 API 验收

**Files:**
- Create: `docs/reports/2026-08-07-structured-extraction-137-acceptance.md`

**Interfaces:**
- Consumes: authorized assets and synthetic fixtures copied to a controlled 137 input root.
- Produces: per-format processor and end-to-end evidence.

- [ ] **Step 1: Copy fixtures safely**

Copy only the selected samples to an allowed 137 input directory, verify sizes and SHA-256, and do not overwrite unrelated files.

- [ ] **Step 2: Run direct processor smoke**

Run MinerU for PDF, PNG, JPG and PPTX; run Docling for DOCX, XLSX, HTML and EPUB. Capture status, duration, output digest and expectation result, not full content.

- [ ] **Step 3: Run production API tasks**

For each format create a unique caller/session/file identity and output directory, poll to terminal state, then validate detected format, processor, routing reasons, target path, Markdown digest/content expectations and manifest.

- [ ] **Step 4: Cover DOCX dual routing**

Verify the ordinary DOCX is routed to Docling and the synthetic complex DOCX is routed to MinerU. If the current deterministic threshold does not produce both branches, record the contradictory evidence and fix the fixture or configuration through a reviewed change rather than falsifying the report.

- [ ] **Step 5: Capture resource and queue evidence**

Record CPU/GPU, memory, temporary disk, Celery backlog, retries and processor slot observations at bounded points during the matrix.

- [ ] **Step 6: Keep failed formats open and report them**

Do not change the allowlist after tests. For failures, classify connection, submission, polling, processing, download, content, publication or system stage and give a recommendation for the user's decision.

### Task 6: 回归、交付审计和提交

**Files:**
- Modify only if evidence requires: files from Tasks 1-2.
- Verify: `docs/superpowers/specs/2026-08-07-structured-extraction-production-formats-design.md`
- Verify: `docs/reports/2026-08-07-structured-extraction-137-acceptance.md`

**Interfaces:**
- Produces: requirement-by-requirement completion evidence.

- [ ] **Step 1: Run local quality gates**

Run focused structured extraction tests, Ruff, applicable type checks, PowerShell syntax checks and `git diff --check`.

- [ ] **Step 2: Re-read production state**

Confirm Docling remains healthy, MinerU remains external and reachable, target units are active, worker settings contain the complete allowlist, and no automatic tightening occurred.

- [ ] **Step 3: Audit every Spec completion criterion**

Map each criterion to current repository, runtime, task, output and report evidence. Missing or indirect evidence remains incomplete.

- [ ] **Step 4: Review repository boundary**

Inspect staged/unstaged/untracked files and scan intended files for secrets. Preserve unrelated user changes.

- [ ] **Step 5: Commit final report and corrections**

```powershell
git add docs/reports/2026-08-07-structured-extraction-137-acceptance.md
git commit -m "验证：记录137结构化提取格式验收"
```

- [ ] **Step 6: Report outcome**

Only claim completion when all eight formats and both DOCX routes have successful content-level production evidence. Otherwise keep the goal active, leave formats open as instructed, and report the exact remaining failures.
