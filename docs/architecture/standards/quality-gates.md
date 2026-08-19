# 工程质量门禁

## 目的

本标准把 ADR 中的工具决策转换为稳定的验证语义。实际命令、版本、路径和阈值由 `pyproject.toml`、`uv.lock`、专用配置、脚本和 CI 工作流维护。文档中的目标工具只有在配置与命令真实存在并成功运行后，才可作为通过证据。

## 结果语义

- `pass`：要求的检查在声明范围内实际执行并成功，证据可定位到提交、环境和命令。
- `fail`：检查已执行并发现问题，或结果超过明确阈值。
- `incomplete`：工具、环境、配置、扫描范围或证据缺失，无法得出通过结论。

只有 `pass` 可以放行。必需检查的 `fail` 和 `incomplete` 都应阻断对应门禁。

## 统一入口

根目录 `justfile` 是质量、测试和构建的稳定公开入口。所有具有范围的命令必须显式指定 `backend`、`classification`、`datajuicer`、`frontend` 或 `all` target；禁止接受任意目录或根据当前目录隐式选择。完整接口和参数约束见 [Justfile Command Harness 设计](../../superpowers/specs/2026-08-06-justfile-command-harness-design.md)。

在 Justfile 尚未真实实现前，应按当前原生配置逐项执行并报告，不能虚构统一入口已经通过。实现后 GitHub Actions 应调用相同 recipe，不在 YAML 中维护第二套工具命令。

| 入口 | 使用时机 | 必需检查 |
| --- | --- | --- |
| `format TARGET SCOPE` | 本地显式格式化 | changed/diff 处理变化文件，full 处理完整 target；该入口允许修改文件 |
| `check TARGET SCOPE` | 本地、提交与 PR | 增量 Ruff 加 target 级类型/Import Linter，或 full 完整门禁；只读 |
| `security TARGET MODE` | 提交、PR、发布或审计 | 按 diff/full/release/history 固定范围执行 Gitleaks、Bandit、Trivy |
| `test-unit TARGET MODE` | 本地、PR 或定期深度测试 | 按 quick/ci/stress 固定范围执行 pytest、Hypothesis 和适用覆盖率 |
| `test-integrate TARGET` | 定时或手动 | 完整 Pact 与 Testcontainers 集成测试 |
| `test-e2e all` | 定时或手动 | 完整系统 Playwright E2E 与适用生成测试 |
| `test-mut TARGET` | 定时或手动 | mutmut 配置范围内的完整变异测试 |
| `test-benchmark TARGET MODE` | 定时、手动或专项诊断 | baseline/profile/load 的固定性能证据 |
| `build-wheel/build-docker` | 构建候选 | 产物校验和 manifest；不发布、不推送 |

## 工具职责

| 关注点 | 目标工具 | 权威配置或证据 |
| --- | --- | --- |
| 环境与依赖 | uv、`uv.lock` | 各锁定单元的 `pyproject.toml`、`uv.lock` |
| 格式与风格 | `ruff format`、`ruff check` | Ruff 配置与命令输出 |
| 复杂度 | Ruff | Ruff 复杂度规则和阈值 |
| 类型 | Pyright；迁移期保留现有 Mypy/ty | 各类型检查器原生配置和零错误输出 |
| 领域依赖 | Import Linter | 架构契约配置及 `lint-imports` 输出 |
| Python 安全 | Bandit | Bandit 配置和扫描报告 |
| 凭据泄露 | Gitleaks | 配置、扫描范围、提交范围和报告 |
| 依赖/镜像/IaC | Trivy | 锁文件、产物摘要、镜像 digest 和报告 |

## 变更范围

- 修改 Python 代码至少运行 Ruff、当前类型门禁和相关 pytest。
- 修改领域分层或模块导入时必须运行 Import Linter。
- 修改依赖、锁文件、Dockerfile、Compose 或 CI 时必须运行对应 Trivy 扫描。
- 修改认证、URI、文件处理、子进程、反序列化或密码相关代码时必须运行 Python 安全扫描和针对性测试。
- 修改凭据模式、部署变量或 CI 权限时必须运行 Gitleaks，并检查日志和产物是否泄露敏感数据。
- 修改任务、存储或发布逻辑时，测试必须覆盖重复消息、入队失败、worker 中断、路径逃逸、输出冲突和恢复。

## 增量检查

`format/check` 的 scope 必须显式选择：`changed` 包含 staged、unstaged 和 untracked 文件；`diff BASE_REF` 使用 merge-base 语义覆盖分支提交和当前工作区；`full` 覆盖完整 target。Ruff 可按 changed/diff 文件执行，Pyright 和 Import Linter 必须保持完整 target 检查。

`changed/diff` 成功只生成 `incremental` evidence，适合本地和 PR 快速反馈，不能替代发布或完整合并门禁。完整证据要求显式运行 `just check all full`。删除和重命名必须由增量文件解析器正确处理；没有适用文件时应报告 `not_applicable`，不能伪造工具已执行。

## 基线与豁免

- 新工具初次接入可以记录已有问题基线，但新改动不得扩大基线。
- 豁免必须包含规则、文件或范围、理由、负责人和失效条件；不接受无边界的全局忽略。
- 自动生成代码、vendor 和迁移文件只能通过明确路径排除，不能用宽泛模式隐藏业务代码。
- 工具间重复规则应指定唯一阻断来源，其他结果作为补充证据。

## 当前迁移边界

仓库当前已有 uv、锁文件、Ruff、pytest、coverage、Mypy/ty 和 Playwright 等配置或依赖。Pyright、Import Linter、Bandit、Gitleaks、Trivy、Justfile 及其统一入口需在后续实施中逐项验证；在此之前相应项应报告为未实施或 `incomplete`，而不是 `pass`。
