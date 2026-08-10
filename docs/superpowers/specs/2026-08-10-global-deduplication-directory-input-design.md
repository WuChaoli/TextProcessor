# 全局去重目录输入与文件迁移设计

## 目标与范围

将现有全局去重的“JSON 清单输入、JSON 结果输出”契约升级为“本地批次目录输入、重复文件迁移”契约。调用方提交一个本地批次目录；worker 从其中的 `original/` 读取文本文件，沿用现有全局近似去重处理器和代表文件选择规则，将被判定为重复的文件迁入 `duplicate/`，并在 `original/` 保留唯一文件。

本变更只升级现有 `global-deduplication` 能力，不新增平行接口，也不兼容旧 JSON 清单输入或 JSON 结果文件。FastAPI、PostgreSQL、Redis、Celery、认证、任务状态机、现有去重处理器和异步 POST/GET 交互模式继续复用。

## API 契约

接口路径保持不变：

```http
POST /api/v1/global-deduplication/tasks
GET  /api/v1/global-deduplication/tasks/{taskId}
```

POST 请求体改为：

```json
{
  "sessionId": "batch-20260810141554",
  "inputPath": "/data/shineData/hub/20260810141554"
}
```

- `inputPath` 是本地绝对目录或等价的 `file://` URI；不再接受 HTTP、S3 或 JSON 清单文件。
- 批次目录必须已有直接子目录 `original/` 与 `duplicate/`。目录不存在、不可访问或二者重叠时，任务失败。
- 幂等键仍为 `(callerId, sessionId)`，请求指纹由规范化后的 `inputPath` 生成；同一键但目录不同返回 `409 IDEMPOTENCY_CONFLICT`。
- 数据库将保存批次根目录，不保存扫描清单、文件正文或对外 JSON 结果。
- 旧请求字段 `inputJsonPath`、`targetPath` 由 API schema 拒绝，不保留兼容分支。

GET 的成功 `result` 改为任务摘要，不再返回 `targetPath`：

```json
{
  "totalFiles": 120,
  "uniqueFiles": 83,
  "movedDuplicates": 35,
  "moveFailures": [
    {"relativePath": "report.txt", "code": "MOVE_FAILED"}
  ]
}
```

`relativePath` 相对 `original/`，不得返回宿主机绝对路径。`moveFailures` 只表示单文件迁移问题；扫描、去重完成后，任务仍为 `succeeded`。

## 扫描与去重

worker 递归枚举 `original/`，以稳定的相对路径顺序构造内部输入。标准目录布局是扁平的，但递归扫描保留对异常子目录布局的容忍性。

- 仅常规 `.md`、`.txt` 与 `.json` 文件参与处理。
- 其他文件类型静默跳过：不进入总数、去重处理或失败列表。
- 不跟随符号链接。
- 无法读取、超出资源限制或不符合目录契约属于任务级失败，不启动文件迁移。
- 内部继续使用 staging 将扫描结果适配成既有处理器输入；处理器的近似去重、重复组和 `keep` 决策不改变。

处理器的 `keep=true` 文件留在 `original/`；只有 `keep=false` 文件进入迁移流程。

## 扁平迁移与乐观成功

每个重复文件的目标固定为 `duplicate/<文件名>`，不保留来源子目录结构。例如 `original/a/report.txt` 的目标是 `duplicate/report.txt`。

- 优先使用同文件系统的原子 rename。
- 若因跨设备无法 rename，则执行“复制到目标临时文件、校验、原子落位、删除源文件”的回退流程。
- 目标同名文件已存在时不得覆盖或改名：源文件保留在 `original/`，记录 `OUTPUT_CONFLICT`，然后继续其他文件。
- 其他迁移错误同样保留源文件，记录 `MOVE_FAILED`，然后继续。
- 成功移动的文件保留在 `duplicate/`；本任务不要求批次级回滚。

任务采用乐观成功语义：只要扫描和去重成功，哪怕部分重复文件迁移失败，任务状态仍为 `succeeded`，并通过 `moveFailures` 说明未完成项。

## 恢复与一致性

在处理器返回后，staging 要持久化本次去重决策、待迁移文件清单和每项迁移状态。任务到达终态后才清理 staging。

- 每成功迁移一个文件，立即持久化对应状态与进度。
- worker 中断、租约失效或重复 Celery 消息后，恢复流程只继续尚未完成的迁移，不重新扫描，也不重新选择代表文件。
- 发现已有目标且能证明它是本任务已完成的同一迁移时，将该文件视为完成；不能证明时记录 `OUTPUT_CONFLICT`。
- 任务级处理器、数据库或基础设施错误仍遵循既有有限重试、失败和恢复规则。

## 安全与可观测性

目录访问继续受服务运行账户 OS 权限、ACL 和 sandbox 约束。路由负责认证、请求校验和协议适配，worker 负责扫描、处理和迁移。

日志记录 `request_id`、`task_id`、调用方标识、阶段和稳定错误码；不记录文件内容、绝对路径、凭据或内部堆栈。公开响应只返回相对文件路径和安全错误码。

## 验收与测试

至少覆盖以下场景：

- 新 POST/GET schema、旧 JSON 字段拒绝、幂等和调用方隔离。
- `original/`、`duplicate/` 目录契约，以及递归扫描、稳定排序和符号链接不跟随。
- `.md/.txt/.json` 处理与其他类型静默跳过。
- 保留唯一文件、扁平移动重复文件、既有同名目标冲突与不覆盖。
- 跨设备迁移回退、单文件移动失败后的乐观成功及安全失败摘要。
- worker 中断、重复消息和恢复后不重新决策、不中断已完成迁移。
- 任务级扫描、读取、处理器和基础设施错误仍为失败，并且 staging 在终态清理。
