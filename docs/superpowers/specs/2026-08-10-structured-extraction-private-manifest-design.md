# 结构化提取私有 Manifest 生命周期设计

## 1. 背景与问题

当前结构化提取任务将固定名称的 `manifest.json` 发布到 `targetPath` 的父目录，并在发布前将该文件视为目录级占用标记。因此，同一目录成功生成 `/runtime/output/1.md` 后，再生成 `/runtime/output/2.md` 会被错误判定为 `OUTPUT_CONFLICT`。

这把任务内部恢复元数据暴露成了调用方产物，也使互不相关的目标文件共享同一个冲突点。固定名称还会在并发任务之间造成覆盖风险。调用方真正需要的最终产物只有其指定的 Markdown 文件；任务状态、结果引用和校验信息由 PostgreSQL 持久化。

## 2. 目标

- `manifest.json` 仅作为非终态任务的内部恢复数据，不发布到用户输出目录；
- 每个任务拥有独立 manifest，消除并发覆盖和目录级冲突；
- 同一目录可以连续或并发发布不同的 `targetPath`；
- 仍维持目标文件默认不覆盖的语义；
- worker 中断和重复投递后能够安全恢复或重新执行；
- 任务进入 `succeeded`、`failed` 或 `cancelled` 后清理其 staging 数据；
- API 请求和响应结构保持不变。

## 3. 非目标

- 不为调用方提供新的 manifest 下载接口；
- 不允许覆盖已经存在的目标 Markdown；
- 不删除或迁移历史输出目录中已有的旧版 `manifest.json`；
- 不改变结构化提取的格式路由、处理器选择或内容规范化逻辑；
- 不以 manifest 代替 PostgreSQL 的任务状态权威性。

## 4. 选定方案

采用任务私有 staging manifest。每个任务在服务端受控 staging 根目录下使用独立目录：

```text
{staging_root}/
  {task_id}/
    source/original
    processor/result.md
    output/result.md
    manifest.json
```

`manifest.json` 不使用隐藏文件名来伪装公开产物，因为整个 `{task_id}` 目录已经位于调用方不可见的内部 staging 区。任务 ID 必须来自服务端任务记录，路径仍需经过现有 staging 根路径约束，不能由请求参数控制。

manifest 只保存恢复所需的最小信息，包括任务 ID、schema version、输入摘要、处理阶段、临时结果路径、目标路径、结果摘要以及必要的 processor/profile 信息。凭据、原始正文和不必要的宿主机信息不得写入 manifest 或日志。

## 5. 数据流与状态顺序

### 5.1 正常成功

1. 创建任务私有 staging 目录并准备输入；
2. 处理器生成临时 Markdown，完成内容和格式校验；
3. 在私有 staging 中写入 manifest，记录即将发布的目标和结果摘要；
4. 对目标 Markdown 执行文件级冲突检查；
5. 将已校验 Markdown 原子发布到 `targetPath`；
6. 回读或校验最终文件，确认摘要与 manifest 一致；
7. 将 PostgreSQL 任务状态及结果引用更新为 `succeeded`；
8. 删除整个任务 staging 目录。

最终输出目录只包含调用方指定的 Markdown，不包含 manifest、隐藏 sidecar、临时文件或任务目录。

### 5.2 失败与取消

任务转为 `failed` 或 `cancelled` 后删除整个任务 staging 目录。清理失败只产生带 `task_id` 的内部告警和可观测指标，不得把已经确定的业务终态改写为另一个结果。

处理失败且尚未发布时不产生最终 Markdown。若发布之后、数据库终态提交之前发生异常，则按恢复规则核验已发布文件，不能直接覆盖或误报普通输出冲突。

### 5.3 中断与恢复

`pending`、`queued` 和 `running` 等非终态任务允许暂时保留私有 manifest。worker 重启或重复消息到达时，以 PostgreSQL 状态为权威，并结合私有 manifest 和最终目标文件执行幂等恢复：

- 目标不存在：从安全阶段继续，或清理不完整临时数据后重新执行；
- 目标存在且摘要与本任务 manifest 一致：视为本任务已经完成发布，补写数据库成功状态后清理 staging；
- 目标存在但摘要不一致：返回真正的 `OUTPUT_CONFLICT`，不得覆盖；
- 数据库已经是终态：不再重新处理，只尝试回收残留 staging。

服务启动或定期清理任务可以回收终态任务遗留的 staging。非终态数据的过期和恢复必须结合数据库状态判断，不能仅按文件年龄盲目删除。

## 6. 输出冲突语义

冲突粒度从“目标父目录”收缩为“本次目标文件”：

- `/runtime/output/1.md` 已成功生成后，可以继续生成 `/runtime/output/2.md`；
- 两个任务并发写入不同目标文件时互不阻塞；
- `targetPath` 本身已存在且不属于本任务的已验证发布结果时，返回 `OUTPUT_CONFLICT`；
- 输出目录中存在旧版 `manifest.json` 时，新任务忽略该文件，不将其作为冲突依据；
- 路径 allowlist、路径逃逸防护和默认不覆盖策略保持不变。

不在输出目录改用 `.manifest-{task_id}.json` 等隐藏 sidecar，因为这仍会污染调用方目录、留下清理负担，并使内部恢复协议成为外部文件布局的一部分。

## 7. 数据与兼容性

PostgreSQL 继续保存任务状态、输入摘要、最终结果引用、结果摘要、错误摘要和生命周期字段。新任务不再把公开 manifest 路径作为结果契约；任务查询不能依赖终态后已删除的 staging manifest。

历史数据库记录和历史输出中的 `manifest.json` 保持原样，不做破坏性迁移。旧记录若带 manifest 引用，查询逻辑应允许其继续存在；新任务不再新增公开 manifest 引用。API 请求与响应字段不变，调用方无需调整参数。

现有文档和项目规则中“最终输出包含 `manifest.json`”的描述需要在实施时同步更新为“manifest 是非终态任务的内部恢复数据”。其中包括根 `AGENTS.md`、结构化提取设计、运行手册和相关测试断言，避免旧契约重新进入实现。

## 8. 可观测性与清理

新增或保留以下内部观测信息：

- staging 创建、恢复和清理事件，携带 `task_id`，不记录文档正文；
- 清理失败计数和终态 staging 残留数量；
- 恢复时的目标摘要匹配、不匹配和重新执行结果；
- 真正的目标文件冲突与旧版 manifest 被忽略的行为。

清理应删除准确解析后的 `{staging_root}/{task_id}`，不得对 staging 根目录或未验证的计算路径执行递归删除。定期清理同样先核对数据库终态，再处理对应任务目录。

## 9. 测试范围

### 9.1 单元测试

- manifest 路径位于任务私有 staging，且不同 task ID 路径不同；
- 发布器只检查目标 Markdown，不检查父目录的 `manifest.json`；
- 目标已存在时仍返回 `OUTPUT_CONFLICT`；
- 成功、失败和取消终态触发 staging 清理；
- 清理失败产生告警但不改变任务终态；
- 新任务的结果元数据不包含公开 manifest 引用。

### 9.2 集成与恢复测试

- 同一目录连续发布 `1.md` 和 `2.md`，两次均成功；
- 并发任务发布不同目标，manifest 不覆盖且结果互不干扰；
- 输出目录存在旧版 `manifest.json` 时仍能发布新目标；
- worker 在发布前中断后可以继续或安全重跑；
- worker 在发布后、数据库成功提交前中断，恢复时通过摘要确认并完成任务；
- 重复消息不会重复发布或破坏已有结果；
- 目标内容与本任务摘要不一致时明确返回冲突；
- 用户输出目录不出现 manifest、隐藏 sidecar或临时文件；
- 终态残留 staging 可由安全清理流程回收。

## 10. 生产发布与验收

1. 修改私有 manifest、文件级冲突检查、恢复和清理实现；
2. 同步项目规则、架构说明、运行手册和测试契约；
3. 完成单元测试以及任务恢复、并发发布集成测试；
4. 本地验证同目录连续生成 `1.md` 和 `2.md`；
5. 部署到 137 服务器并滚动重启实际承载结构化提取的 API 和 worker；
6. 在生产接口使用两个不同 `targetPath` 执行冒烟测试；
7. 核验最终输出、PostgreSQL 状态、worker 日志和 staging 清理情况。

生产验收标准：不同 `targetPath` 可以连续或并发处理；只有目标文件本身已存在且不属于本任务的幂等恢复时才冲突；输出目录只有请求指定的 Markdown；任务进入任一终态后不保留 manifest。发布前保留可回滚版本，若恢复逻辑异常则回滚应用，不删除已经生成的 Markdown。
