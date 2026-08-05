# ADR-0001：后台业务任务采用单一队列所有者

- 状态：已接受
- 日期：2026-08-05
- 决策者：TextProcessor 项目维护者

## 背景

TextProcessor 通过 FastAPI 接收请求，通过 PostgreSQL 保存任务状态，并通过 Redis 与 Celery 执行后台任务。Docling 和 Data-Juicer 等自有能力服务也分别引入了 RQ 或 Celery，使一个业务任务形成多层异步链路：

```text
FastAPI -> TextProcessor Celery -> 能力服务 API -> 能力服务队列 -> 算法执行
```

这种双层队列适合由不同团队维护、供多个系统共享并需要独立扩缩容的平台型服务，但本项目主要采用单机单实例部署。重复的队列、任务 ID、状态机、轮询、重试和恢复机制增加了部署与故障定位成本，也可能产生重试叠加和状态不一致。

同时，FastAPI 接口进程仍需与后台任务进程隔离。接口服务故障不应中断已经入队或正在执行的后台任务。

## 决策

一个后台业务任务只允许一个持久队列所有者。TextProcessor Celery 是自有后台业务任务的唯一队列，PostgreSQL 是任务状态的唯一权威来源。

运行职责划分如下：

- `backend-api` 独立运行 FastAPI，负责鉴权、请求校验、任务落库、Celery 入队、同步能力调用和任务查询。
- `task-runner` 独立于 API，在一个容器内运行 Celery Worker 与 Celery Beat，负责后台任务编排、状态转换、超时、有限重试、恢复、输入输出处理和结果发布。
- 自有能力容器提供同步、无状态的内部算法接口，不持有业务任务队列或权威任务状态。
- CPU/GPU 密集算法由对应能力容器执行；Task Runner 不直接承载重型算法。
- Redis DB 0 承载 TextProcessor Celery。移除 Docling RQ 后，不再为 Docling 保留 Redis DB 1。
- API 与 Task Runner 分别重启。Task Runner 暂时不可用时，只要 PostgreSQL 与 Redis 可用，API 仍接受后台任务、持久化并返回 `202 + taskId`，等待 Task Runner 恢复后处理。

典型后台任务链路为：

```text
Client
  -> backend-api
  -> PostgreSQL + Redis/Celery
  -> task-runner
  -> Docling 或 Data-Juicer 同步内部 API
  -> task-runner 校验并发布结果
  -> PostgreSQL 最终状态
```

轻型且有明确超时上限的能力继续使用同步接口。例如分类请求由 `backend-api` 调用 Classification 内部 API 并等待结果，不创建后台任务。

## 适用边界与例外

- Docling 删除内部 RQ 队列，由 Task Runner 调用其同步转换接口。
- Data-Juicer 删除内部 Celery 队列，由 Task Runner 调用其同步处理接口。
- Classification 已是同步推理能力，保持独立容器和同步内部接口。
- Plain Text、Markdown Cleaning 等依赖轻量的后台处理可直接在 Task Runner 内执行，不必为算法额外创建容器。
- 无法控制且只提供异步协议的第三方引擎可以保留 `submit + externalTaskId + poll` 适配。该外部任务不是 TextProcessor 内部的第二层队列，Task Runner 仍负责本地状态、超时和恢复。
- 若未来某项自有能力需要成为多调用方共享平台，必须通过新的 ADR 明确其独立任务所有权、状态边界和迁移方案，不能直接叠加第二层队列。

## 后果

### 正面影响

- 删除重复的队列、任务状态、轮询和恢复实现。
- 缩短自有能力的任务链路，降低端到端延迟和运维复杂度。
- 所有后台任务使用一致的幂等、重试、超时、恢复和审计规则。
- API 故障与后台任务执行互不影响。
- 能力容器仍保留依赖、资源和算法故障隔离。

### 代价与约束

- 调用同步能力接口期间会占用 Celery Worker slot，因此不同资源类型需要使用明确的队列、并发数和硬超时。
- Task Runner 承担更多编排职责，必须保持处理器 adapter 边界，不能吸收 CPU/GPU 密集算法。
- 能力容器异常时，由中央 Celery 策略负责重试；能力接口自身不得再进行不可见的无限重试。
- 长任务必须验证 Worker 丢失、能力容器中断、结果发布冲突和恢复后的幂等行为。

## 被否决的方案

### 保留两层队列

故障隔离和独立扩缩容更强，但不符合当前单机单实例部署的简化目标，并造成重复状态与恢复机制。

### 让每个能力服务拥有自己的队列

中央 Task Runner 可以变成纯转发器，但任务可靠性与状态模型会分散到多个服务，长期形成不同队列技术和协议的长尾。

### 将重型算法直接放入 Task Runner

链路最短，但会把模型、GPU 和算法依赖耦合进任务编排容器，降低故障隔离并扩大镜像与资源占用。
