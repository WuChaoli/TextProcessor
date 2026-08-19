# 测试策略

## 原则

- 测试按风险选择，不要求每项功能机械覆盖所有测试类型。
- 优先在最低成本且足够真实的层次发现问题，但关键跨进程和恢复行为必须在真实边界验证。
- mock、fake、内存实现、容器依赖、局域网服务、真实 GPU/模型和生产等价环境必须明确区分。
- 未运行、被跳过或因环境缺失而无法运行的测试，不得报告为通过。

## 测试层级

| 层级 | 工具 | 主要用途 | 默认门禁 |
| --- | --- | --- | --- |
| 单元测试 | pytest | domain、application、processor、状态机、策略和错误映射 | 本地、PR；quick/ci/stress |
| 属性测试 | Hypothesis + pytest | URI/路径、状态转换、schema、manifest、幂等不变量 | 随单元或系统测试的固定 profile |
| 集成测试 | pytest + Testcontainers for Python | PostgreSQL、Redis、Celery、MinIO、`fsspec` 等真实边界 | 定时或手动完整执行 |
| 契约测试 | Pact | Gateway 与 Capability Service 的消费者/提供者契约 | 随完整集成测试执行 |
| API 测试 | pytest/FastAPI、Apifox | 路由行为、鉴权、错误码、OpenAPI 和环境冒烟 | PR 或专项环境冒烟 |
| 变异测试 | mutmut | 核心领域规则的测试有效性 | 定时或手动完整执行 |
| 局部性能 | `pytest-benchmark` | 稳定纯函数或 processor 的回归比较 | `baseline` |
| 服务性能 | k6 | API 吞吐、延迟、并发、异步提交/查询与容量 | `load` |
| 性能诊断 | memray、py-spy | 内存分配和 CPU 栈定位 | `profile` |
| 浏览器 E2E | Playwright | 已确认的真实用户旅程 | 定时或手动完整执行 |
| 覆盖率 | `pytest-cov` | 未测试区域和分支提示 | 单元测试 `ci` |

## 统一测试入口

测试通过根 Justfile 的稳定 recipe 执行：`test-unit TARGET MODE`、`test-integrate TARGET`、`test-e2e all`、`test-mut TARGET` 和 `test-benchmark TARGET MODE`。target 是受控构建单元，不接受任意目录；缺失 target 或 mode 时默认阻断。

只有单元测试提供 `quick/ci/stress`，因为它们对应本地、PR 和定期属性压力测试的真实范围差异。集成、E2E 和变异测试主要由定时或手动 GitHub Actions 执行，必须运行各自配置的完整集合，不提供可被调用方缩小的档位。详细参数见 [Justfile Command Harness 设计](../../superpowers/specs/2026-08-06-justfile-command-harness-design.md)。

## TextProcessor 必测风险

- processor：正常样例、边界输入、错误映射、确定性和幂等。
- URI 策略：协议、根路径、host/CIDR、bucket、大小、超时、重定向、符号链接和路径逃逸。
- 任务状态机：所有合法转换、非法转换、并发竞争、重复消息和取消。
- 发布与 manifest：临时/最终隔离、摘要验证、原子发布、默认不覆盖、同摘要恢复和冲突。
- Celery：入队失败、重复投递、有限重试、worker 中断、租约过期、心跳与新 worker 接管。
- API：鉴权、调用方隔离、幂等冲突、稳定错误码，以及响应不泄露正文、凭据、堆栈和绝对路径。
- 观测：日志脱敏、关联标识传播、指标标签基数和 trace context 传播。

## 现实度与环境

每次集成、性能或 E2E 报告至少记录：提交、执行命令、测试层级、依赖类型、镜像或服务版本、关键环境参数、通过/失败/跳过数量及清理结果。使用 Testcontainers 只能证明容器化依赖边界；真实局域网服务、GPU 模型和部署拓扑仍需独立验证。

## 性能测试

- k6 场景必须声明目标接口、数据集、并发模型、持续时间、预热、超时、错误率和延迟预算。
- 后台任务分别度量提交延迟、排队时间、处理时间和端到端完成时间，不把 `202` 接受响应视为任务完成。
- `pytest-benchmark` 只比较稳定的局部代码路径；CI 硬件不稳定时先作为趋势证据，不直接设置脆弱的阻断阈值。
- 性能回归必须与可复现基线比较，不能跨机器或跨数据集直接下结论。

## 覆盖率与变异

覆盖率阈值写入 `pyproject.toml` 或 CI，不在本文固定。关键风险即使达到行覆盖率，也应通过分支断言、属性测试或 mutmut 验证测试是否能发现错误。变异存活项应按业务风险处理，不追求无差别的百分之百 mutation score。

## 当前迁移边界

pytest、coverage 和前端 Playwright 已存在于仓库；Hypothesis、Testcontainers、Pact、mutmut、k6、`pytest-benchmark` 和 `pytest-cov` 的统一配置仍需实施和验证。`coverage` 当前存在不等于 `pytest-cov` 已经接入。
