# Classification Service

受控的文本分类推理服务独立运行环境。

配置通过 `CLASSIFICATION_` 前缀的环境变量提供。模型发布目录必须位于配置的模型根目录下；生产环境只接受 `production-approved` 发布物。

默认开发验证不会加载真实模型：

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/test_config.py -v
uv run --project services/classification_service ruff check services/classification_service/classification_service services/classification_service/tests
uv run --project services/classification_service mypy services/classification_service/classification_service
```

真实验收只在授权 RTX 3090、单逻辑 CUDA GPU、不可变 release 和受控非敏感参考 fixture 均已配置时运行：

```powershell
uv run --project services/classification_service pytest services/classification_service/tests/real_integration -m real_integration -v
```

参考 fixture 由 `CLASSIFICATION_REFERENCE_FIXTURE` 指向，包含参考算法生成的 token chunk ids、两个逐片段概率矩阵、标签、四项 tags 和两个 confidence。比较容差固定为 `rtol=1e-6, atol=1e-8`。缺少真实条件时测试明确 skip，交付结论必须写 `real_integration not run`。
