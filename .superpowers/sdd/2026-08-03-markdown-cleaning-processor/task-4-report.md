## Task 4: Presidio allowlist 与五类脱敏（Final）

### Result: DONE

- **RED**
  - 补齐 `CN Mobile ID/IPv4/Bank Card/Email` 的识别与校验分支，修复 Presidio 可选依赖下的静态类型边界与 fallback 兼容。
  - 先前存在以下问题：
    - `cn_recognizers.py` 中条件分支类别名导致 mypy/pyright 的多类型重绑定告警。
    - `presidio_adapter.py` 的主路径构建包含不必要 `type: ignore` 与异常后 fallback 分支判定不清晰。

- **GREEN**
  - 已完成类型与行为收敛，主路径/异常路径语义固定为：
    - 有主路径可用（`_HAS_PRESIDIO=True` 且 analyzer 可建）时，返回 `used_fallback=False`。
    - 主路径执行异常时，直接返回原文（非 fallback）并填充 `fallback_error=敏感数据脱敏失败`，不做掩码重写。
    - 主路径不可用时走本地 fallback 规则并写 `used_fallback=True`。
  - 导出接口稳定：`CNIDCardRecognizer`、`CNMobilePhoneRecognizer`、`IPv4AddressRecognizer` 在 `processors/__init__.py` 中可导出。

### Presidio 主路径与 registry/no-download 证据

- 主路径构造依赖 `NoOpNlpEngine`，不依赖模型下载：
  - [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
    - `NoOpNlpEngine([{"lang_code": "en", "model_name": "noop-en"}])`
    - `AnalyzerEngine(registry=..., nlp_engine=...)`
- Registry 注册证据（主路径）：
  - 同文件 `_build_registry()` 先后添加：
    - `EmailRecognizer`（若可用）
    - `CreditCardRecognizer`（若可用）或 `_FallbackCreditCardRecognizer`
    - `CNMobilePhoneRecognizer`、`CNIDCardRecognizer`、`IPv4AddressRecognizer`
- no-download/本地化证据：
  - 测试 `test_presidio_main_path_uses_noop_nlp_and_allowlist_registry` 断言 `analyzer.nlp_engine.models == [{"lang_code": "en", "model_name": "noop-en"}]`，且 registry 包含固定 recognizer 名单（无远程拉模型）。

### 异常不 fallback 语义证据

- [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
  - `redact()` 在 `except` 中：
    - `if self._analyzer is not None:` 分支返回 `text=markdown, used_fallback=False`（保留原文）；
    - 仅当 `self._analyzer is None` 时执行 `_fallback_matches()` 并返回 `used_fallback=True`。
- [backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py)
  - `test_redact_runtime_analyze_error_maps_to_sensitive_error_without_fallback` 断言异常时 `used_fallback is False` 且 `text == markdown`，`fallback_error == "敏感数据脱敏失败"`。

### 验证与证据

- `uv run ruff check backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run mypy backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run ty check backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run pyright backend/app/features/markdown_cleaning/processors/cn_recognizers.py backend/app/features/markdown_cleaning/processors/presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py`
- `uv run pytest backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py`（设置 `PROJECT_NAME/POSTGRES_SERVER/POSTGRES_USER/FIRST_SUPERUSER/FIRST_SUPERUSER_PASSWORD`，结果：9 passed）

### 文件变更

- [backend/app/features/markdown_cleaning/processors/__init__.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/__init__.py)（本任务导出改动）
- [backend/app/features/markdown_cleaning/processors/cn_recognizers.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/cn_recognizers.py)
- [backend/app/features/markdown_cleaning/processors/presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/app/features/markdown_cleaning/processors/presidio_adapter.py)
- [backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_presidio_adapter.py)
- [backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py](C:/Users/wuchaoli/Desktop/codespace/TextProcessor/.worktrees/markdown-cleaning-api/backend/tests/features/markdown_cleaning/processors/test_cn_recognizers.py)

### 备注

- 该任务提交状态最终为 DONE；未发现阻断性风险；已保留现有导出兼容与 API 契约。

