# ADR-0002：Python 工程质量与供应链工具链

- 状态：已接受
- 日期：2026-08-06

## 背景

TextProcessor 同时包含 FastAPI 主应用、Celery worker 和独立能力服务。质量检查需要覆盖代码风格、类型、复杂度、模块边界、代码安全、依赖与镜像安全以及凭据泄露，并在本地和 CI 中使用同一套可复现环境。

本 ADR 只确定工具职责和采用方向。具体版本、规则、排除项和命令以各项目的 `pyproject.toml`、`uv.lock`、专用配置和 CI 工作流为事实来源；工具被列入本 ADR 不表示已经安装或已经成为阻断门禁。

## 决策

1. Python 环境与依赖统一由 `uv` 管理。每个独立锁定单元维护自己的 `pyproject.toml` 和 `uv.lock`；CI 使用冻结锁文件安装，不在验证期间隐式更新依赖。

2. `ruff format` 是 Python 格式化工具，`ruff check` 负责风格、常见缺陷、导入顺序和复杂度规则。复杂度检查优先使用 Ruff 的 McCabe `C901` 等规则，并把阈值写入实际配置，不在本文固化数值。

3. Pyright 作为目标静态类型门禁。现有 Mypy 和 ty 在 Pyright 配置、基线和 CI 验证完成前继续保留；迁移必须显式完成，不能仅凭本文删除已有门禁。长期是否保留多类型检查器由后续证据决定。

4. Import Linter 负责验证领域与模块依赖契约，例如 presentation 不得绕过 application 直接依赖 infrastructure。架构契约写入 Import Linter 的原生配置，不由 Ruff 或类型检查替代。

5. 安全工具按职责分工：

   - Gitleaks 扫描 Git 历史和待提交变更中的凭据及高风险秘密。
   - Bandit 扫描 Python 特有的安全风险。与 Ruff `S` 规则重叠时，由配置明确唯一阻断来源，避免同一问题重复报告。
   - Trivy 扫描锁文件、文件系统、容器镜像和适用的 IaC 配置。依赖漏洞判断必须基于锁定后的实际依赖或构建产物。

6. 新工具分阶段接入：先建立可重复命令和基线，再进入 CI 观察，最后才升级为阻断门禁。缺少工具、配置、运行证据或完整扫描范围时，结果是 `incomplete`，不得记为通过。

7. 根目录 `justfile` 作为工程质量、测试和构建的统一公开入口。调用方必须显式指定稳定 target；target 映射到受控目录、工具链和能力，不接受任意目录。`format/check` 还必须选择 changed、diff 或 full scope，增量通过不能替代完整 target 门禁。Justfile 只负责编排，工具规则仍由原生配置维护。

## 结果

- 工具规则留在原生配置中，ADR 只解释长期决策和职责边界。
- `uv.lock` 是版本解析结果的权威来源，但不能替代漏洞扫描或许可证审查。
- 同一仓库中的独立能力服务可以使用不同 Python 版本或独立锁文件，但必须满足相同的质量门禁语义。
- 引入 Pyright、Import Linter、Bandit、Gitleaks 或 Trivy 需要单独实施和验证；本文不宣称这些工具当前已经运行。

## 关联标准

- [工程质量门禁](../standards/quality-gates.md)
- [测试策略](../standards/testing-strategy.md)
- [Justfile Command Harness 设计](../../superpowers/specs/2026-08-06-justfile-command-harness-design.md)
