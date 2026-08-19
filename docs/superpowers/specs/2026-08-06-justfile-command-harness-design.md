# TextProcessor Justfile Command Harness 设计

- 状态：已确认
- 日期：2026-08-06
- 范围：统一开发、质量、安全、测试与构建入口；不包含工具安装、GitHub Actions 实现、发布、推送或部署

## 1. 目标

TextProcessor 使用根目录 `justfile` 提供稳定的仓库级命令接口。调用方只选择构建单元和具有明确业务意义的执行模式，不直接拼接 Ruff、Pyright、pytest、Pact、Playwright、mutmut、k6、memray、py-spy、uv 或 Docker 命令。

Justfile 是调度入口，不是配置权威。工具规则、测试集合、阈值和版本仍由 `pyproject.toml`、`uv.lock`、工具原生配置、测试 marker、构建配置和项目脚本维护。

## 2. 设计原则

1. 默认 recipe 只显示帮助，不执行格式化、检查、测试或构建。
2. 所有具有作用范围的 recipe 必须显式提供 `TARGET`；不根据当前目录或变更文件隐式选择目标。
3. 公开接口使用稳定 target，而不是任意目录。目录、工具链、锁文件、测试能力、wheel 能力和 Docker 服务是 target registry 的内部属性。
4. `all` 是允许的显式 target，但调用方必须写出；不存在默认 `all`。
5. `format` 和 `check` 必须显式选择 `changed`、`diff` 或 `full` 范围；增量通过只代表增量证据，不能替代完整 target 门禁。
6. 只有具有实际执行差异的命令才提供 `MODE`。集成、E2E 和变异测试使用固定完整语义，不提供 smoke/ci/full 等档位。
7. 正式入口不接受任意参数透传，防止调用方用 pytest 选择器或扫描排除项缩小范围后仍把结果报告为完整通过。
8. GitHub Actions 只负责触发、Runner、秘密、环境和产物上传；实际工具调用复用相同 Just recipe。
9. 构建入口只生成和验证本地产物，不 push、不 publish、不部署，也不创建 Git tag。

## 3. Target registry

首版 target 固定为：

```text
backend
classification
datajuicer
frontend
all
```

registry 至少描述：

| Target | Path | Toolchain | Capabilities |
| --- | --- | --- | --- |
| `backend` | `backend/` | 根 uv workspace 与根锁文件 | format、check、security、unit、integration、mutation、benchmark、wheel、docker |
| `classification` | `services/classification_service/` | 独立 uv 项目与锁文件 | format、check、security、unit、integration、mutation、benchmark、wheel、docker |
| `datajuicer` | `services/datajuicer_service/` | 独立 uv 项目与锁文件 | format、check、security、unit、integration、mutation、benchmark、wheel、docker |
| `frontend` | `frontend/` | Bun | format、check、docker；作为系统 E2E 驱动端 |
| `all` | 聚合目标 | 各目标自己的工具链 | recipe 允许的全部适用目标 |

`TARGET` 是稳定构建单元标识，不是路径别名。禁止把绝对路径、仓库外路径、vendor 路径或任意相对目录作为 target。未来移动目录时只更新 registry，不修改公开命令。

## 4. 公开接口

```text
just
just list
just doctor TARGET

just format TARGET SCOPE [BASE_REF]
just check TARGET SCOPE [BASE_REF]
just security TARGET MODE [BASE_REF]

just test-unit TARGET MODE
just test-integrate TARGET
just test-e2e TARGET
just test-mut TARGET
just test-benchmark TARGET MODE

just build-wheel TARGET
just build-docker TARGET TAG
```

### 4.1 帮助与诊断

- `just` 与 `just list` 展示公开 recipe、合法 target/mode 和可复制示例。
- `doctor TARGET` 只读验证 target 所需工具、锁文件、配置、Docker 能力和必要环境，不安装依赖、不修改配置、不启动长期进程。

### 4.2 格式与快速校验

```text
format TARGET changed
format TARGET diff BASE_REF
format TARGET full

check TARGET changed
check TARGET diff BASE_REF
check TARGET full
```

| Scope | 文件集合 | 证据用途 |
| --- | --- | --- |
| `changed` | 当前工作区 staged、unstaged 和 untracked 的适用文件 | 本地编辑快速反馈 |
| `diff BASE_REF` | 从 `merge-base(BASE_REF, HEAD)` 到当前工作区的已提交、staged、unstaged 和 untracked 变化 | 分支或 PR 增量反馈 |
| `full` | target 的全部适用文件 | 完整质量门禁 |

`format` 执行实际格式化；Python 使用 `ruff format`，frontend 使用其原生格式化入口。`check` 是只读入口：Ruff 格式检查和 lint/复杂度在 `changed/diff` 下只处理选出的适用文件；Pyright 和 Import Linter 始终检查完整 target，因为类型影响和领域依赖契约不能可靠缩小为文件级证据。frontend 中能够安全按文件运行的 formatter/linter 使用增量文件，项目级类型检查仍覆盖完整 target。

`diff` 必须显式提供 `BASE_REF`，`changed/full` 禁止提供该参数。删除文件不传给文件扫描器，但仍计入 target 级 Pyright 和 Import Linter 的完整检查。没有适用变更文件时，增量文件检查报告 `not_applicable`，不能虚构执行成功；target 级检查仍按契约运行。

Pyright 接管门禁前，现有 Mypy/ty 继续按迁移计划保留，Justfile 不因目标设计已接受而提前删除现有检查。`changed/diff` 的 `pass` 必须标记为 `incremental` evidence；发布或完整质量门禁只接受 `check all full` 的完整证据。

### 4.3 安全检查

```text
security TARGET diff BASE_REF
security TARGET full
security all release
security all history
```

| Mode | 范围 |
| --- | --- |
| `diff` | Gitleaks 扫描相对显式 `BASE_REF` 的差异；Bandit 扫描目标变更代码；Trivy 扫描变更涉及的锁文件和 IaC |
| `full` | 当前工作树和分支、目标完整生产源码、目标文件系统、锁文件和 IaC |
| `release` | 全仓发布提交、全部生产源码、锁文件、IaC 和已构建发布镜像 |
| `history` | 完整 Git 历史秘密扫描，加上 release 的完整代码、依赖、IaC 和镜像范围 |

`diff` 必须显式提供 `BASE_REF`，其他模式禁止提供该参数。`release` 和 `history` 只接受 `TARGET=all`。安全入口不隐式构建镜像；发布镜像必须来自与当前提交匹配的构建 manifest，否则结果为 `incomplete`。

### 4.4 单元测试

```text
test-unit TARGET quick
test-unit TARGET ci
test-unit TARGET stress
```

| Mode | 固定语义 |
| --- | --- |
| `quick` | 核心单元测试和小规模 Hypothesis，用于本地快速反馈 |
| `ci` | 完整单元测试、标准 Hypothesis profile 和覆盖率门禁 |
| `stress` | 完整单元测试及大规模 Hypothesis 状态空间，用于定期深度验证 |

Hypothesis 的 `max_examples`、deadline、健康检查和失败样例数据库策略写入原生 profile。Justfile 只选择 profile，不保存具体数值。mock/fake 测试必须归类为单元测试，不能作为真实集成证据。

### 4.5 固定完整测试

- `test-integrate TARGET` 执行目标完整的 Pact 与 Testcontainers 集成测试，包括真实 PostgreSQL、Redis、Celery、MinIO、`fsspec` 边界，以及适用的重复投递、失败恢复、worker 中断、路径逃逸、输出冲突和清理验证。
- `test-e2e all` 执行完整系统 E2E；该 recipe 只接受 `TARGET=all`。Playwright 驱动已确认用户旅程，Hypothesis 只生成受约束的系统输入或任务序列，失败样例必须可保存和重放。
- `test-mut TARGET` 对目标配置范围内的全部适用生产代码执行 mutmut。范围和排除项由 mutmut 原生配置确定，不允许调用方临时缩小。

集成、E2E 和变异测试主要由定时或手动 GitHub Actions 触发，因此不提供额外 mode。缺少 Docker、浏览器、真实依赖、清理证据或完整测试集合时，不得降级为小范围测试并报告通过。

### 4.6 性能测试与诊断

```text
test-benchmark TARGET baseline
test-benchmark TARGET profile
test-benchmark all load
```

| Mode | 工具与产物 |
| --- | --- |
| `baseline` | Python benchmark 工具运行固定数据、预热和采样，输出可比较的 JSON 基线 |
| `profile` | memray 和 py-spy 对指定场景生成内存报告与 CPU 火焰图 |
| `load` | k6 对完整服务入口执行固定吞吐、延迟、错误率和容量场景 |

`load` 只接受 `TARGET=all`。`baseline` 是性能回归证据，`profile` 是诊断证据，两者不能互相替代。负载测试必须通过受控配置提供目标 URL、环境授权和预算，禁止根据默认值误压未知服务或生产环境。

### 4.7 构建

- `build-wheel TARGET` 只接受 Python target 或 `all`，分别在正确锁定单元使用 uv 构建并验证 wheel metadata。
- `build-docker TARGET TAG` 构建明确目标和 tag 的镜像，不允许省略 `TAG`，不隐式使用或覆盖 `latest`。
- `build-docker all TAG` 按固定规则为各镜像派生唯一 tag，并记录实际镜像 digest。

构建产物写入受控目录并生成 manifest。manifest 至少包含 commit、dirty 状态、target、产物路径或镜像引用、digest、工具链和生成时间。安全 release/history 只接受 commit 匹配且完整的 manifest。

## 5. 参数校验矩阵

| Recipe | 合法 TARGET | 其他参数约束 |
| --- | --- | --- |
| `doctor` | 任意 target；`all` 必须显式 | 无 |
| `format`、`check` | 任意 target；`all` 必须显式 | SCOPE=`changed/diff/full`；diff 要求 `BASE_REF` |
| `security` | Python target 或 `all`；release/history 仅 `all` | `diff/full/release/history` |
| `test-unit` | Python target 或 `all` | `quick/ci/stress` |
| `test-integrate`、`test-mut` | Python target 或 `all` | 无 |
| `test-e2e` | 仅 `all` | 无 |
| `test-benchmark` | Python target 或 `all`；load 仅 `all` | `baseline/profile/load` |
| `build-wheel` | Python target 或 `all` | 无 |
| `build-docker` | 任意 target | 无；`TAG` 必填 |

非法 target、非法 mode、缺失参数或多余参数必须在启动工具前失败，并输出合法值和一个正确示例。

## 6. 配置与安全边界

命令行参数只承载非敏感选择：`TARGET`、`SCOPE`、`MODE`、`BASE_REF` 和 `TAG`。`BASE_REF` 只用于 `format/check ... diff` 和 `security ... diff`，且必须由调用方显式提供。环境地址、token 和外部环境授权来自受控环境变量或 GitHub Actions secrets。Justfile 和脚本不得打印秘密值。

Pact broker 发布不属于 `test-integrate` 默认行为。外部 E2E 或 k6 必须同时具备目标地址和显式授权信号；缺少任一项时在发出网络请求前失败。普通验证入口不得发布契约、推送镜像、部署服务或修改共享环境。

## 7. Justfile 与脚本边界

Justfile 负责公开 recipe、参数枚举、target registry 查询、调度、退出码保留和帮助。Git diff 解析、报告聚合、容器生命周期、清理验证、manifest 生成和跨平台错误处理放入 `scripts/project/` 的可测试脚本。

建议结构：

```text
justfile
scripts/project/
├── README.md
├── doctor.*
├── check.*
├── security.*
├── test-unit.*
├── test-integrate.*
├── test-e2e.*
├── test-mut.*
├── test-benchmark.*
├── build-wheel.*
└── build-docker.*
```

脚本语言和最终文件拆分在实施计划中根据 Windows 本地与 Linux GitHub Runner 的共同支持情况确定。本 Spec 不预先承诺未经验证的脚本运行时。

## 8. 结果与退出码

统一摘要至少包含 command、target、scope、mode、base ref、commit、worktree 状态、result、evidence class、artifact paths 和 duration。结果语义为：

| Exit code | Result | 含义 |
| --- | --- | --- |
| `0` | `pass` | 必需步骤在声明范围内实际成功 |
| `1` | `fail` | 检查、测试或阈值发现问题 |
| `2` | invalid | 参数或调用组合无效 |
| `3` | `incomplete` | 工具、环境、服务或输入证据缺失 |
| `4` | `incomplete` | 清理或产物验证不完整 |

`all` 聚合全部适用 target。实现应保留各 target 证据并在最终汇总后返回整体结果；任何必需 target 的 `fail` 或 `incomplete` 都阻断整体通过。必需测试被跳过、外部服务不可达或清理失败时不得返回 `pass`。

## 9. GitHub Actions 边界

PR 工作流可以调用 `check TARGET diff BASE_REF` 提供快速增量反馈，但合并所需完整质量门禁必须调用 `check all full`；同时调用 `security ... full` 和 `test-unit ... ci`。集成、E2E、变异和负载测试由定时或 `workflow_dispatch` 工作流调用固定完整入口：

```text
just test-integrate all
just test-e2e all
just test-mut all
just test-benchmark all load
```

Actions 可以使用 target matrix 降低单 job 时间，但不能在 YAML 中复制或改变原生工具参数。工作流汇总必须区分 `fail` 与 `incomplete`，并上传相应报告和清理证据。

## 10. 非目标

- 不在本阶段安装或配置所列质量工具。
- 不在本阶段创建 Justfile、registry、项目脚本或 GitHub Actions。
- 不提供任意路径执行、任意工具参数透传或自动 target 推断。
- 不提供 push、publish、deploy、数据库重置、环境销毁或 Git 发布操作。
- 不以本文替代各工具的原生配置、版本锁定、测试场景和性能预算。

## 11. 实施验收

后续实现至少验证：帮助输出、每个合法 target、缺失 target、非法 target、非法 scope/mode、diff 缺失或错误 base ref、staged/unstaged/untracked/删除/重命名文件集合、多余参数、`all` 聚合、工具缺失、Docker 缺失、外部授权缺失、测试失败、必需测试跳过、清理失败、产物 manifest 不匹配，以及真实的 changed/diff/full check、test 和 build 路径。GitHub Actions 必须实际调用 Just recipe，不能保留并行的重复工具命令作为另一权威入口。
