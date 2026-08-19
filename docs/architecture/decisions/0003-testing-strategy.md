# ADR-0003：分层测试与验证工具链

- 状态：已接受
- 日期：2026-08-06

## 背景

TextProcessor 的风险不仅来自纯函数，还来自 PostgreSQL、Redis、Celery、`fsspec`、HTTP/S3、浏览器流程以及内部服务契约。单一测试框架或全部依赖 mock 的测试不能证明系统可恢复、可部署或满足性能预算。

## 决策

1. pytest 是 Python 测试的统一运行入口，`pytest-cov` 负责覆盖率采集。覆盖率用于发现未验证区域，不以单一总百分比代替关键行为断言。

2. Hypothesis 用于具有稳定不变量和大输入空间的领域规则，例如状态机转换、URI 策略、路径规范化、schema 和 manifest 校验。普通示例测试仍保留，用于表达关键业务案例。

3. Testcontainers for Python 用于需要真实 PostgreSQL、Redis、MinIO 等依赖的可重复集成测试。测试必须区分真实容器依赖与 fake/mock，不得把 fake 测试报告为真实集成测试。

4. Pact 用于 Text Processing Gateway 与独立 Capability Service 之间的消费者驱动契约测试。Pact 不替代 FastAPI 路由功能测试、OpenAPI/Apifox 契约同步或真实服务冒烟。

5. mutmut 用于核心领域逻辑的变异测试，优先覆盖状态机、URI 策略、幂等、发布冲突和 manifest。变异测试不进入每次提交的快速门禁，而进入定期或 release 验证。

6. k6 用于 HTTP API、异步任务提交与查询链路的负载和容量验证。Python 局部性能回归使用 `pytest-benchmark`。两类结果必须绑定固定场景、环境、数据集、并发和预算，不能跨环境直接比较。

7. Playwright 用于浏览器 E2E。当前前端任务管理界面不在业务建设承诺内，因此只维护模板现有流程和未来明确验收的用户旅程，不为尚未设计的界面预写 E2E。

8. 单元测试通过 `quick`、`ci`、`stress` 表达真实执行范围。集成、E2E 和变异测试使用固定完整语义，主要由定时或手动 GitHub Actions 调用，不设置没有实际范围差异的 smoke/ci/full 档位。真实大型模型、GPU、局域网服务和外部系统测试必须单独标识并记录运行环境。

9. k6 通过统一性能入口的 `load` 模式运行；局部基线和 memray/py-spy 诊断分别使用 `baseline` 与 `profile`，三类证据不能互相替代。

## 结果

- 各测试层回答不同问题，不能用低现实度测试替代高现实度验证。
- 新增 Hypothesis、Testcontainers、Pact、mutmut、k6 或 `pytest-benchmark` 需要单独实施；本文不表示当前已经具备相应依赖、配置和场景。
- 测试失败为 `fail`；必需环境、工具或证据缺失为 `incomplete`。两者都不能作为发布通过证据。

## 关联标准

- [测试策略](../standards/testing-strategy.md)
- [工程质量门禁](../standards/quality-gates.md)
- [Justfile Command Harness 设计](../../superpowers/specs/2026-08-06-justfile-command-harness-design.md)
