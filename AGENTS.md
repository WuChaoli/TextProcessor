# TextProcessor 项目执行说明

## 项目定位

- 本项目面向内部调用方提供文本结构化提取、文本清洗和文本分类服务。
- 保留当前 FastAPI、React、PostgreSQL、Docker 与 Traefik 全栈模板；业务建设以 backend 为主，前端任务管理界面另行设计，不在当前边界内承诺。
- 生产服务只接入已验证的处理器和模型。模型训练、数据集制作、算法实验与评测平台不属于本项目。

## 目标架构

- 采用模块化单体：FastAPI API 与 Celery worker 位于同一仓库、共享领域模型和应用代码，但作为独立进程启动和扩缩容。
- 秒级且有明确超时上限的操作由 `async` FastAPI 接口等待并直接返回；长耗时、批量、资源密集或需要可靠重试的操作通过后台 task 接口执行。
- 每项能力的执行模式由 API 契约预先确定，不根据单次运行耗时在同步与后台模式间自动切换。需要时同一能力可提供显式的 execute 和 task 两类入口。
- Redis 作为 Celery broker；PostgreSQL 是任务状态的权威来源。Celery 消息只携带 `task_id`、任务类型和 schema version，完整任务参数从 PostgreSQL 读取。
- API route 只负责鉴权、校验和协议适配，不包含算法实现。结构化提取、清洗和分类通过稳定的 processor 接口接入，本地实现与外部引擎均置于 adapter 边界之后。

## 输入、输出与存储

- 使用 `fsspec` 统一访问输入和输出。首版支持受控的 `file://`、局域网 `http(s)://` 与 MinIO `s3://`；其他协议需单独设计和授权。
- 本地绝对路径由 API/worker 运行账号的 OS 权限、ACL 与 systemd sandbox 决定，不设置应用层 roots allowlist；HTTP/S3 URI 仍须通过协议、host/CIDR、bucket、大小和超时 allowlist。调用方不得通过请求扩大文件系统、网络或凭据权限，URI 中不得携带凭据。
- `http(s)` 输入只读；本地文件与 MinIO 可作为生产输出。请求可指定经过校验的 `output_uri`。
- 后台任务使用按 task ID 隔离的内部 staging；`manifest.json` 只在非终态期间用于恢复，任务进入终态后清理，不发布到调用方输出目录。
- 临时输出与最终输出分离；只有结果校验成功后才原子发布调用方指定的结果文件，避免消费者读取半成品。默认不覆盖已有目标文件。
- PostgreSQL 只保存任务状态、输入摘要、结果引用、错误摘要和生命周期字段，不保存大文件或大段正文。

## 任务、安全与可靠性

- 任务状态遵循显式状态机，例如 `pending -> queued -> running -> succeeded|failed|cancelled`；禁止绕过合法转换。
- 入队、重复投递、worker 中断、超时和重试必须可恢复。处理器和输出发布必须幂等；重试次数有限，不得无限重试。
- 同步接口不得在事件循环中直接执行 CPU/GPU 密集工作；使用受控执行器或改用后台任务。
- 处理接口采用服务端身份认证。任务记录调用方身份，用于查询隔离、幂等和审计；首版不建设复杂 RBAC、计费或租户平台。
- 对外错误使用稳定的 error code，区分请求、输入访问、处理、输出和系统错误。响应与日志不得泄露凭据、内部堆栈或宿主机绝对路径。
- 日志携带 `request_id`、`task_id` 和调用方标识，但默认不记录原始文本、文档内容或其他敏感数据。

## 开发与验证边界

- 修改前读取真实代码、配置和测试，遵循现有 backend/frontend 分层；不因模板存在而假设业务能力已经实现。
- 生产代码不得直接依赖 `DatasetTechTest` 等实验项目；可借鉴其已验证实现，但必须经 processor/adapter 边界迁入并补齐项目内测试。
- 优先小步建设可运行、可观察、可验证的能力，不提前拆分微服务、独立仓库或复杂工作流平台。
- 按变更范围执行格式化、Lint、类型检查和测试。processor、URI 策略、任务状态机与 manifest 至少具备单元测试。
- API、PostgreSQL、Redis、Celery 与 `fsspec` 边界使用集成测试；任务相关改动必须覆盖重复消息、入队失败、worker 中断、路径逃逸、输出冲突和恢复。
- 真实大型模型或外部服务测试与默认快速测试集分离，未实际运行的验证不得声称通过。

## Apifox CLI 项目关联

- Apifox 权威项目为 `TextProcessor API`，Team ID `4055426`，Project ID `8681977`。
- 局域网环境为 `生产环境（局域网）`，Environment ID `48037649`；端到端冒烟场景 ID 为 `8601238`。
- API 契约以当前代码和 FastAPI OpenAPI 为事实来源；接口契约变化后使用 `apifox` CLI 同步接口、中文字段说明和相关测试用例。
- 创建或更新复杂资源前先执行对应的 `apifox cli-schema get` 和 `apifox cli-schema validate`，写入后通过 `get` 或 `list` 回读验证。
- 环境变量只写入目标 Apifox 环境，不创建同名项目全局变量；测试提取器使用 `environment` 作用域。
- 不在 AGENTS.md、README、Git 或命令输出中记录 Apifox Access Token、生产密码或其他真实凭据。

## 非目标

- 不建设模型训练、数据集管理、实验管理或模型评测平台。
- 不建设长期文档资产管理、搜索、知识库或内容运营平台。
- 不允许任意协议、任意宿主机路径、任意公网 URL 或调用方提供的访问凭据。
- 不把实现步骤、临时 TODO、依赖版本清单或易变化的 API schema 固化在本文件中；这些内容应进入对应代码、测试、配置或专项设计文档。
