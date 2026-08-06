# Classification Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成独立部署的 SetFit 双模型 Classification Service，按固定切片与聚合契约返回四项 `tags` 和两个独立 confidence，并具备不可变模型 release、单 GPU有界推理及内部 HTTP协议。

**Architecture:** 服务是第三层无业务状态 Capability Service，使用 DDD/端口适配器分层。FastAPI只处理内部认证与协议；application编排一次切片和 `top-triple-classifier -> end-doc-classifier` 串行推理；SetFit、release文件、线程执行器均位于 infrastructure。服务拥有独立 Python 3.12环境和锁文件，不导入 TextProcessor backend 或 DatasetTechTest。

**Tech Stack:** Python 3.12、FastAPI、Pydantic Settings、SetFit 1.1.3、Sentence Transformers 5.6.1、Transformers 4.49.0、Torch 2.13.0、scikit-learn 1.9.0、NumPy 1.26.4、pytest、Ruff、mypy、ty、uv、Docker/NVIDIA CUDA。

## Global Constraints

- 模型正式名称固定为 `top-triple-classifier` 和 `end-doc-classifier`；生产代码、配置、指标和 API 不使用 A/B 命名。
- `tags` 固定四项：三级主题路径在前，文档类型在后；两个模型始终返回最高分结果，不做置信度阈值过滤。
- `confidence` 是片段 `predict_proba` 后预测类别的文档级算术平均概率，不声明为校准概率。
- 切片固定 `maxLength=256`、`overlap=32`、`maxChunksPerDocument=16`、`selection=uniform`、`aggregation=arithmetic_mean`。
- 单实例固定一个 Uvicorn worker、一个推理线程、一个 active inference、八个 waiting requests、15秒软预算。
- 生产强制单张 CUDA GPU，不允许自动回退 CPU；CPU只用于 fake 单元测试。
- 模型从只读不可变 release 加载；禁止在线下载、请求选版本、热切换和未知版本回退。
- 当前 baseline `20260729T093134Z-321175f0` 只能标记 `experimental`，不得部署到 production。
- 正文、绝对路径、服务令牌和内部堆栈不得写入日志或公开错误。
- 默认测试不得加载真实大模型；真实模型与 GPU测试使用 `real_integration` 标记并单独运行。

---

## File Structure

本计划创建：

```text
services/classification_service/
|-- pyproject.toml
|-- uv.lock
|-- Dockerfile
|-- classification_service/
|   |-- __init__.py
|   |-- main.py
|   |-- bootstrap.py
|   |-- domain/
|   |-- application/
|   |-- infrastructure/
|   `-- presentation/
|-- tools/
|-- tests/
`-- README.md
```

沿用现有 `services/datajuicer_service` 的 flat package 约定，不在该服务内改成 `src/` layout，避免同仓库出现第三种构建方式。根 `pyproject.toml` 将它加入 workspace `exclude`，服务通过自身 `uv.lock` 管理 Python 3.12依赖。

---

### Task 1: 独立服务骨架、配置与质量门禁

**Files:**
- Modify: `pyproject.toml`
- Create: `services/classification_service/pyproject.toml`
- Create: `services/classification_service/classification_service/__init__.py`
- Create: `services/classification_service/classification_service/infrastructure/config.py`
- Create: `services/classification_service/tests/test_config.py`
- Create: `services/classification_service/README.md`
- Create: `services/classification_service/uv.lock`（由 `uv lock` 生成）

**Interfaces:**
- Produces: `Settings`、`RuntimeEnvironment`、`QualityStatus`、`get_settings()`。
- Consumes: 无。

- [ ] **Step 1: 写配置失败测试**

在 `tests/test_config.py` 固定以下行为：

```python
def test_production_rejects_experimental_release(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="production-approved"):
        Settings(
            environment="production",
            internal_service_token=SecretStr("secret"),
            model_root=tmp_path,
            model_release=tmp_path / "release",
            model_release_sha256="a" * 64,
            release_quality_status="experimental",
        )


def test_inference_capacity_is_fixed() -> None:
    settings = valid_settings()
    assert settings.inference_workers == 1
    assert settings.active_inference_limit == 1
    assert settings.waiting_queue_limit == 8
    assert settings.inference_timeout_seconds == 15
    assert settings.max_text_chars == 500_000
```

- [ ] **Step 2: 运行测试确认因模块不存在而失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/test_config.py -v`

Expected: FAIL，`classification_service.infrastructure.config` 不存在。

- [ ] **Step 3: 创建独立项目与最小配置**

`services/classification_service/pyproject.toml` 固定 Python和核心模型版本；dev group包含 pytest、Ruff、mypy、ty。配置使用 `CLASSIFICATION_` 前缀，并定义：

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLASSIFICATION_", extra="ignore")

    environment: Literal["development", "staging", "production"]
    internal_service_token: SecretStr
    model_root: Path
    model_release: Path
    model_release_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_quality_status: Literal["experimental", "production-approved"]
    max_text_chars: int = 500_000
    inference_workers: Literal[1] = 1
    active_inference_limit: Literal[1] = 1
    waiting_queue_limit: int = 8
    inference_timeout_seconds: float = 15.0
    minimum_free_gpu_mib: int = 8192
```

production拒绝 experimental；`model_release` 必须位于 `model_root` 下。根 `pyproject.toml` 的 workspace exclude 改为：

```toml
exclude = ["services/datajuicer_service", "services/classification_service"]
```

- [ ] **Step 4: 生成锁文件并运行配置测试与静态检查**

Run:

```powershell
uv lock --project services/classification_service
uv run --project services/classification_service pytest services/classification_service/tests/test_config.py -v
uv run --project services/classification_service ruff check services/classification_service/classification_service services/classification_service/tests
uv run --project services/classification_service mypy services/classification_service/classification_service
```

Expected: 全部 PASS；`uv.lock` 固定解析版本。

- [ ] **Step 5: 提交骨架**

```powershell
git add pyproject.toml services/classification_service
git commit -m "feat: 建立分类服务独立运行环境"
```

---

### Task 2: 领域模型与公开传输类型

**Files:**
- Create: `services/classification_service/classification_service/domain/errors.py`
- Create: `services/classification_service/classification_service/domain/label_path.py`
- Create: `services/classification_service/classification_service/domain/model_identity.py`
- Create: `services/classification_service/classification_service/domain/classification_result.py`
- Create: `services/classification_service/classification_service/application/dto.py`
- Create: `services/classification_service/tests/domain/test_classification_result.py`

**Interfaces:**
- Produces: `TopTriplePath`, `ModelPrediction`, `ClassificationResult`, `ClassifyTextCommand`。
- Consumes: Task 1配置常量。

- [ ] **Step 1: 写领域失败测试**

```python
def test_compose_fixed_four_tags() -> None:
    result = ClassificationResult.compose(
        top_triple=ModelPrediction("应急 > 安全生产 > 危化品", 0.72),
        end_doc=ModelPrediction("法规标准类", 0.81),
        release_id="release-1",
    )
    assert result.tags == ("应急", "安全生产", "危化品", "法规标准类")
    assert result.top_triple_confidence == 0.72
    assert result.end_doc_confidence == 0.81


@pytest.mark.parametrize("label", ["", "应急 > 安全生产", "a > b > c > d"])
def test_top_triple_requires_exactly_three_non_empty_levels(label: str) -> None:
    with pytest.raises(DomainValidationError):
        TopTriplePath.from_leaf_label(label)
```

另测 NaN、Inf、负数、大于1、空 end-doc、相邻空层级。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/domain/test_classification_result.py -v`

Expected: FAIL，领域类型尚未定义。

- [ ] **Step 3: 实现不可变领域类型**

核心签名固定为：

```python
@dataclass(frozen=True)
class ModelPrediction:
    label: str
    confidence: float


@dataclass(frozen=True)
class ClassificationResult:
    tags: tuple[str, str, str, str]
    top_triple_confidence: float
    end_doc_confidence: float
    release_id: str

    @classmethod
    def compose(
        cls,
        *,
        top_triple: ModelPrediction,
        end_doc: ModelPrediction,
        release_id: str,
    ) -> Self:
        path = TopTriplePath.from_leaf_label(top_triple.label)
        return cls(
            tags=(*path.levels, end_doc.label),
            top_triple_confidence=top_triple.confidence,
            end_doc_confidence=end_doc.confidence,
            release_id=release_id,
        )
```

`TopTriplePath.from_leaf_label()` 只按字面分隔符 ` > ` 拆分并要求恰好三级。

- [ ] **Step 4: 运行领域测试**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/domain -v`

Expected: PASS。

- [ ] **Step 5: 提交领域模型**

```powershell
git add services/classification_service/classification_service/domain services/classification_service/classification_service/application/dto.py services/classification_service/tests/domain
git commit -m "feat: 定义双模型分类领域结果"
```

---

### Task 3: Token切片与文档级分数聚合

**Files:**
- Create: `services/classification_service/classification_service/application/ports/text_chunker.py`
- Create: `services/classification_service/classification_service/infrastructure/model/tokenizer_chunker.py`
- Create: `services/classification_service/classification_service/infrastructure/model/score_aggregator.py`
- Create: `services/classification_service/tests/infrastructure/test_tokenizer_chunker.py`
- Create: `services/classification_service/tests/infrastructure/test_score_aggregator.py`

**Interfaces:**
- Produces: `TextChunker.chunk(text) -> tuple[str, ...]`、`aggregate_scores(scores, labels) -> ModelPrediction`。
- Consumes: Task 2 `ModelPrediction`。

- [ ] **Step 1: 写切片与聚合失败测试**

使用可记录 token ids 的 fake tokenizer，覆盖短文本、special token预算、32 overlap、超过16窗口时首/中/尾均匀索引、空token与空decode。聚合核心断言：

```python
def test_aggregates_chunk_probabilities_by_arithmetic_mean() -> None:
    scores = np.asarray([[0.8, 0.2], [0.4, 0.6]], dtype=np.float64)
    prediction = aggregate_scores(scores, ("first", "second"))
    assert prediction.label == "first"
    assert prediction.confidence == pytest.approx(0.6)
```

另测 shape、NaN、Inf、越界概率和并列时取标签顺序第一项。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_tokenizer_chunker.py services/classification_service/tests/infrastructure/test_score_aggregator.py -v`

Expected: FAIL，切片器和聚合器不存在。

- [ ] **Step 3: 迁入最小确定性算法**

从实验算法重新实现 `_window_starts()`、`_uniform_indices()` 和 tokenizer encode/decode；不导入实验项目。配置类型：

```python
@dataclass(frozen=True)
class ChunkingConfig:
    max_length: int = 256
    overlap: int = 32
    max_chunks_per_document: int = 16
```

聚合要求 `scores.shape == (len(chunks), len(labels))` 的上层契约，并返回平均矩阵 argmax 的 `ModelPrediction`。

- [ ] **Step 4: 运行测试、格式化与类型检查**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_tokenizer_chunker.py services/classification_service/tests/infrastructure/test_score_aggregator.py -v
uv run --project services/classification_service ruff format --check services/classification_service
uv run --project services/classification_service ruff check services/classification_service
uv run --project services/classification_service mypy services/classification_service/classification_service
```

Expected: PASS。

- [ ] **Step 5: 提交推理算法**

```powershell
git add services/classification_service
git commit -m "feat: 实现文档切片与概率聚合"
```

---

### Task 4: 不可变模型 Release 契约与离线工具

**Files:**
- Create: `services/classification_service/classification_service/infrastructure/release/manifest.py`
- Create: `services/classification_service/classification_service/infrastructure/release/checksum.py`
- Create: `services/classification_service/classification_service/infrastructure/release/validator.py`
- Create: `services/classification_service/tools/package_release.py`
- Create: `services/classification_service/tools/validate_release.py`
- Create: `services/classification_service/tests/infrastructure/test_release_validator.py`
- Create: `services/classification_service/tests/test_release_tools.py`

**Interfaces:**
- Produces: `ReleaseManifest.load(path)`、`validate_release(settings) -> ValidatedRelease`。
- Consumes: Task 1 environment配置；Task 2模型身份。

- [ ] **Step 1: 写 release 安全失败测试**

构造纯文本 fake release，覆盖：manifest SHA256不匹配、文件缺失、未登记文件、checksum错误、符号链接逃逸、18/6标签数错误、非三级 top label、production加载 experimental、目标打包目录已存在。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_release_validator.py services/classification_service/tests/test_release_tools.py -v`

Expected: FAIL，release模块不存在。

- [ ] **Step 3: 实现 schema v1 与全文件校验**

`ReleaseManifest` 使用 Pydantic `extra="forbid"`，模型 key 必须精确等于：

```python
{"top-triple-classifier", "end-doc-classifier"}
```

所有相对路径 resolve 后必须仍在 release root 下；拒绝 symlink。`checksums.sha256` 使用规范化相对 POSIX路径，覆盖除其自身以外的所有 release 文件。打包工具使用临时 sibling目录完成后原子 rename，且目标存在时失败。

- [ ] **Step 4: 运行 release 测试与 CLI smoke**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_release_validator.py services/classification_service/tests/test_release_tools.py -v
uv run --project services/classification_service python services/classification_service/tools/validate_release.py --help
```

Expected: PASS，CLI help退出码0。

- [ ] **Step 5: 提交 release 契约**

```powershell
git add services/classification_service
git commit -m "feat: 增加分类模型发布包校验"
```

---

### Task 5: SetFit双模型适配器与启动加载门禁

**Files:**
- Create: `services/classification_service/classification_service/application/ports/classifier.py`
- Create: `services/classification_service/classification_service/infrastructure/model/setfit_loader.py`
- Create: `services/classification_service/classification_service/infrastructure/model/top_triple_classifier.py`
- Create: `services/classification_service/classification_service/infrastructure/model/end_doc_classifier.py`
- Create: `services/classification_service/classification_service/infrastructure/model/runtime.py`
- Create: `services/classification_service/tests/infrastructure/test_setfit_adapters.py`
- Create: `services/classification_service/tests/real_integration/test_release_inference.py`

**Interfaces:**
- Produces: `Classifier.predict(chunks) -> ModelPrediction`、`LoadedClassificationRuntime`。
- Consumes: Task 3聚合器；Task 4 `ValidatedRelease`。

- [ ] **Step 1: 写 fake SetFit模块失败测试**

fake模型记录 `from_pretrained`路径并返回 tensor-like或 ndarray概率。断言两个 adapter校验 `(chunks,18)`、`(chunks,6)`，执行聚合，标签来自 manifest而不是硬编码顺序；shape和NaN失败。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_setfit_adapters.py -v`

Expected: FAIL，SetFit adapter不存在。

- [ ] **Step 3: 实现延迟导入、CUDA门禁和双模型加载**

使用 `import_module("setfit")`/`import_module("torch")` 隔离测试。启动要求：CUDA可用、只看到一个逻辑设备、逻辑 `cuda:0` 空闲显存不少于8192 MiB。加载顺序固定为 tokenizer、top triple、end doc；使用 `torch.inference_mode()`执行固定 smoke。任何失败抛出稳定 `ModelLoadError`，不得 CPU fallback。

- [ ] **Step 4: 运行 fake 测试，并登记真实测试命令但不伪造结果**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/infrastructure/test_setfit_adapters.py -v`

GPU环境实际执行：

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/real_integration/test_release_inference.py -m real_integration -v
```

Expected: fake测试 PASS。只有在已挂载真实 release、RTX 3090和CUDA环境时 real integration才应执行并记录真实结果；其他环境明确 SKIP。

- [ ] **Step 5: 提交模型适配器**

```powershell
git add services/classification_service
git commit -m "feat: 加载SetFit双模型分类运行时"
```

---

### Task 6: Application用例、专用线程和有界准入

**Files:**
- Create: `services/classification_service/classification_service/application/ports/inference_executor.py`
- Create: `services/classification_service/classification_service/application/classify_text.py`
- Create: `services/classification_service/classification_service/infrastructure/execution/thread_executor.py`
- Create: `services/classification_service/classification_service/infrastructure/execution/admission_controller.py`
- Create: `services/classification_service/tests/application/test_classify_text.py`
- Create: `services/classification_service/tests/infrastructure/test_admission_controller.py`

**Interfaces:**
- Produces: `ClassifyTextHandler.execute(command) -> ClassificationResult`。
- Consumes: Task 2 DTO/domain；Task 3 `TextChunker`；Task 5两个 `Classifier`。

- [ ] **Step 1: 写用例顺序和容量失败测试**

断言正文只规范化/切片一次、top triple先于end doc、任一失败不返回部分结果。使用可控 Future验证第10个请求立即抛 `InferenceCapacityExceeded`，超时后实际线程结束前不释放 active slot，排队取消会移除 waiter。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/application/test_classify_text.py services/classification_service/tests/infrastructure/test_admission_controller.py -v`

Expected: FAIL，用例和准入控制器不存在。

- [ ] **Step 3: 实现单整体阻塞流水线**

核心边界：

```python
class ClassifyTextHandler:
    async def execute(self, command: ClassifyTextCommand) -> ClassificationResult:
        return await self._executor.run(
            lambda: self._classify_blocking(command)
        )
```

专用 `ThreadPoolExecutor(max_workers=1, thread_name_prefix="classification-inference")`。准入容量为 active 1 + waiting 8；15秒从接收准入请求开始计时。超时只结束 await，不取消已运行线程；slot在底层 Future完成回调中释放。

- [ ] **Step 4: 运行并发测试和事件循环响应测试**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/application/test_classify_text.py services/classification_service/tests/infrastructure/test_admission_controller.py -v`

Expected: PASS，测试证明等待期间事件循环仍能运行另一个 coroutine。

- [ ] **Step 5: 提交应用用例**

```powershell
git add services/classification_service
git commit -m "feat: 实现有界单线程分类用例"
```

---

### Task 7: FastAPI内部协议、认证、错误与健康状态

**Files:**
- Create: `services/classification_service/classification_service/presentation/schemas.py`
- Create: `services/classification_service/classification_service/presentation/authentication.py`
- Create: `services/classification_service/classification_service/presentation/error_mapping.py`
- Create: `services/classification_service/classification_service/presentation/routes.py`
- Create: `services/classification_service/classification_service/presentation/health.py`
- Create: `services/classification_service/classification_service/bootstrap.py`
- Create: `services/classification_service/classification_service/main.py`
- Create: `services/classification_service/tests/contract/test_classification_api.py`
- Create: `services/classification_service/tests/integration/test_application_lifecycle.py`

**Interfaces:**
- Produces: `POST /internal/v1/classify`、`GET /health/live`、`GET /health/ready`。
- Consumes: Task 1 Settings；Task 4 release；Task 5 runtime；Task 6 handler。

- [ ] **Step 1: 写HTTP契约失败测试**

覆盖额外字段、schemaVersion、header/body requestId一致性、Bearer token、空正文、500001字符、成功四项tags/两个confidence、400/401/413/429/500/503/504映射、公开错误不泄露正文或路径、ready状态迁移。

- [ ] **Step 2: 运行契约测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/contract/test_classification_api.py -v`

Expected: FAIL，FastAPI app尚不存在。

- [ ] **Step 3: 实现 app factory 与 lifespan**

请求/响应Pydantic模型使用 `extra="forbid"`。内部认证用 `secrets.compare_digest`比较 bearer token。lifespan按 `validating_release -> loading_tokenizer -> loading_top_triple_classifier -> loading_end_doc_classifier -> smoke_testing -> ready` 更新状态；shutdown先停止准入再关闭executor。CUDA OOM映射503、取消ready并触发进程退出钩子。

- [ ] **Step 4: 运行契约、生命周期和全量快速测试**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests -m "not real_integration" -v
uv run --project services/classification_service ruff format --check services/classification_service
uv run --project services/classification_service ruff check services/classification_service
uv run --project services/classification_service mypy services/classification_service/classification_service
uv run --project services/classification_service ty check services/classification_service/classification_service
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交HTTP服务**

```powershell
git add services/classification_service
git commit -m "feat: 提供内部文本分类接口"
```

---

### Task 8: 容器、Compose隔离与运行手册

**Files:**
- Create: `services/classification_service/Dockerfile`
- Modify: `compose.yml`
- Modify: `.env.example`
- Create: `docs/runbooks/classification-service.md`
- Create: `scripts/verify-classification-service.ps1`
- Test: `services/classification_service/tests/test_container_contract.py`

**Interfaces:**
- Produces: 内网 `classification-service:8000`，只读 `/models/releases` 挂载。
- Consumes: Task 7 FastAPI入口。

- [ ] **Step 1: 写容器契约失败测试**

静态解析Dockerfile/Compose，断言单worker、无Traefik外网labels、无PostgreSQL/Redis依赖、模型volume只读、internal token和release配置来自环境、healthcheck指向 `/health/ready`、offline环境变量存在。

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run --project services/classification_service pytest services/classification_service/tests/test_container_contract.py -v`

Expected: FAIL，容器文件尚不存在。

- [ ] **Step 3: 实现GPU容器和Compose服务**

Dockerfile 使用 `nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04`，安装 Python 3.12 与 uv，执行 `uv sync --locked --no-dev`，并固定 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。实现前先运行 `docker manifest inspect nvidia/cuda:13.0.0-cudnn-runtime-ubuntu24.04`；只有镜像清单真实存在且目标 RTX 3090 smoke 通过后才保留该 tag。启动命令：

```text
uvicorn classification_service.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Compose不暴露host端口，不加入 `traefik-public`，不连接db/redis；声明GPU reservation和只读 release volume。`verify-classification-service.ps1`验证live、ready、未认证401、固定smoke请求和日志敏感字段扫描。

- [ ] **Step 4: 验证静态契约和可构建性**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/test_container_contract.py -v
docker compose config --quiet
git diff --check
```

GPU构建环境再运行：

```powershell
docker compose build classification-service
docker compose up -d classification-service
powershell -File scripts/verify-classification-service.ps1
```

Expected: 静态检查 PASS；GPU命令只有在目标CUDA环境和真实development release上实际执行后才能记为通过。

- [ ] **Step 5: 提交部署配置**

```powershell
git add services/classification_service/Dockerfile compose.yml .env.example docs/runbooks/classification-service.md scripts/verify-classification-service.ps1 services/classification_service/tests/test_container_contract.py
git commit -m "build: 增加分类服务GPU部署配置"
```

---

### Task 9: 参考实现一致性与最终验收

**Files:**
- Create: `services/classification_service/tests/real_integration/test_reference_parity.py`
- Modify: `services/classification_service/README.md`
- Modify: `docs/runbooks/classification-service.md`

**Interfaces:**
- Produces: 可审计的真实模型一致性与性能证据。
- Consumes: Tasks 1-8完整服务；外部受控 baseline release和RTX 3090。

- [ ] **Step 1: 写真实一致性测试入口**

测试只从环境变量读取受控 release路径和非敏感 fixture路径；缺少条件时 `pytest.skip`。在参考算法与新服务中比较 chunk ids、两个概率矩阵、平均分、argmax标签、四项tags和两个confidence，使用显式 `rtol=1e-6, atol=1e-8`。

- [ ] **Step 2: 运行默认门禁，确认真实测试不被误执行**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests -m "not real_integration" -q
uv run --project services/classification_service ruff format --check services/classification_service
uv run --project services/classification_service ruff check services/classification_service
uv run --project services/classification_service mypy services/classification_service/classification_service
uv run --project services/classification_service ty check services/classification_service/classification_service
git diff --check
```

Expected: 全部 PASS，真实模型测试未运行且不会被声称通过。

- [ ] **Step 3: 在授权GPU服务器运行真实验收**

Run:

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/real_integration -m real_integration -v
powershell -File scripts/verify-classification-service.ps1
```

记录 releaseId、模型qualityStatus、GPU型号、锁文件SHA256、加载显存、单请求峰值显存、P50/P95/P99和参考一致性结果；不得记录正文。

- [ ] **Step 4: 核对验收结论边界**

只有真实结果满足以下条件才可报告完成：单逻辑GPU、输出与参考容差一致、固定四项tags/两个confidence、推理低于15秒软预算、无正文/绝对路径/令牌日志泄露、experimental release未进入production。未具备GPU或模型时明确报告 `real_integration not run`。

- [ ] **Step 5: 提交真实验收资产**

```powershell
git add services/classification_service/tests/real_integration services/classification_service/README.md docs/runbooks/classification-service.md
git commit -m "test: 增加双模型分类真实验收"
```

---

## Completion Gate

在声称 Classification Service 完成前，重新执行：

```powershell
uv sync --project services/classification_service --locked
uv run --project services/classification_service pytest services/classification_service/tests -m "not real_integration" -q
uv run --project services/classification_service ruff format --check services/classification_service
uv run --project services/classification_service ruff check services/classification_service
uv run --project services/classification_service mypy services/classification_service/classification_service
uv run --project services/classification_service ty check services/classification_service/classification_service
docker compose config --quiet
git diff --check
git status --short
```

如果目标GPU与真实 release可用，再执行 `real_integration` 和部署 smoke；否则交付报告必须明确该门禁未运行。完成本计划不等于 Access API 或 Text Processing Gateway 已实现，它们使用独立计划推进。
