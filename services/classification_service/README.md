# Classification Service

受控的文本分类推理服务独立运行环境。

配置通过 `CLASSIFICATION_` 前缀的环境变量提供。模型发布目录必须位于配置的模型根目录下；生产环境只接受 `production-approved` 发布物。

开发验证：

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/test_config.py -v
uv run --project services/classification_service ruff check services/classification_service/classification_service services/classification_service/tests
uv run --project services/classification_service mypy services/classification_service/classification_service
```
