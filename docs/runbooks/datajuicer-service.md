# Data-Juicer 服务源码运行与验证

## 运行边界

Data-Juicer 服务以独立 Python 3.11 项目运行，API、Celery worker 和 Celery
Beat 使用同一份源码与 PostgreSQL 任务表。Redis 仅作为 Celery broker。

本阶段不构建 Data-Juicer 服务 Docker 镜像。PostgreSQL 和 Redis 可以使用本机
已有实例或测试容器，但服务进程必须通过仓库内源码脚本启动。

## 前置配置

复制 `services/datajuicer_service/.env.example` 并配置：

- `DATAJUICER_DATABASE_URL`：PostgreSQL 连接，数据库须允许执行 Alembic
  migration。
- `DATAJUICER_CELERY_BROKER_URL`：Redis broker URL。
- `DATAJUICER_CELERY_QUEUE`：Data-Juicer 服务独占队列，默认
  `datajuicer.jobs`。

三个服务进程必须使用完全相同的以上配置。

Data-Juicer 与 TextProcessor 可以复用同一个 PostgreSQL 实例，但必须使用
独立 database。两个服务拥有各自的 Alembic 版本表，不能把两个 migration
历史写入同一个 database。

## 启动

在仓库根目录分别开启三个 PowerShell 终端：

```powershell
services/datajuicer_service/scripts/run_api.ps1
services/datajuicer_service/scripts/run_worker.ps1
services/datajuicer_service/scripts/run_beat.ps1
```

API 默认监听 `127.0.0.1:8091`。启动门禁会校验 Python 版本、uv lock、
Data-Juicer 子模块 commit、运行时兼容性和数据库 migration，不会启动或构建
Docker。

检查服务：

```powershell
Invoke-RestMethod http://127.0.0.1:8091/health
Invoke-RestMethod http://127.0.0.1:8091/ready
```

## 固定 Hugging Face 文本验收

真实文本验收使用 Hugging Face 数据集
`fka/awesome-chatgpt-prompts`，固定 revision
`c3064c383a935d8ff5b8d363888e317e4132badc` 和文件 `prompts.csv`。
仓库目前重定向到 `fka/prompts.chat`；脚本会跟随重定向，但仍校验固定 revision，
不会读取移动的 `main`。

```powershell
$env:DATAJUICER_RUN_REAL_HF = "1"
uv run --project services/datajuicer_service pytest `
  -m real_integration `
  services/datajuicer_service/tests/real_integration/test_huggingface_text.py -q
```

也可独立生成可审计报告：

```powershell
uv run --project services/datajuicer_service python `
  services/datajuicer_service/scripts/run_real_text_validation.py `
  --work-dir $env:TEMP/datajuicer-hf-validation/work `
  --cache-dir $env:TEMP/datajuicer-hf-validation/cache `
  --report $env:TEMP/datajuicer-hf-validation/report.json
```

脚本最多下载 2 MiB，缓存固定 revision 的源文件，固定选择源记录索引
`0, 2, 9`，并记录数据集、输入和输出 SHA-256。测试精确断言：

- `0, 1, 2` 属于同一个 `exact_minhash` 组，代表为 `0`；
- `4, 5` 属于同一个 `minhash` 组，代表为 `4`；
- `3` 是独立文档；
- `4, 5` 在固定英文源文本上附加中文段落，覆盖多语言近似去重；
- 输入包含 UTF-8 BOM，输出包含全部六个 uid。

## 2026-07-31 源码端到端证据

在 PostgreSQL 18 测试库与 Redis 7 broker 上，源码启动 API、worker 和 Beat：

- `POST /v1/jobs` 返回 job
  `57f3cb04-3111-49e1-b4c7-6cfb60e3fd58`；
- worker 收到 `datajuicer.execute` 并在约 0.078 秒内完成；
- GET 最终状态为 `succeeded`、阶段 `completed`、进度 100%；
- 输出 6 条记录，分组、方法和代表与固定断言一致；
- 输出 SHA-256 为
  `5b1b2b31f8fc20387e341a365875ea8dde7922eabdc8e30397137955323e7dd5`；
- Beat 每 5 秒投递一次 `datajuicer.recover`，worker 实际消费成功。

验收后已停止 API、worker、Beat，并删除本次临时 Redis 容器；没有生成服务
Dockerfile 或镜像。

## 2026-07-31 TextProcessor 三模块合并证据

> 注意：以下为历史 JSON 清单契约的验收记录。当前全局去重接口改为提交
> `inputPath` 本地批次目录；目录必须含 `original/` 与 `duplicate/`，结果通过
> GET 的 `totalFiles`、`uniqueFiles`、`movedDuplicates`、`moveFailures` 返回。重复
> 文件平铺迁入 `duplicate/`，不再发布业务 JSON 文件。

使用同一 PostgreSQL 18 实例中的两个独立 database、同一 Redis 7 实例中的
隔离队列，以源码进程启动 TextProcessor API/worker 和 Data-Juicer
API/worker：

- TextProcessor `POST /api/v1/global-deduplication/tasks` 创建任务
  `019fb78d-0b62-746f-80ca-89353cba9847`；
- TextProcessor worker 通过真实 HTTP 创建 Data-Juicer job
  `20d3bf43-2243-4c54-ad3e-ae5987f0750f`；
- Data-Juicer worker 完成精确去重，处理器输出 SHA-256 为
  `edeadec2a2cf1ab61a4bf0202fb443699f9a00a1bb3b4060679576a8072b4a01`；
- TextProcessor GET 最终返回 `succeeded`、`completed`、3/3、100%；
- 最终业务 JSON SHA-256 为
  `d8a99a12226d7b88d90f637493bb906edfa29350751f7547b6496261306af056`；
- 三条记录严格只含 `fileId,fileStoragePath,groupId,keep`，保留决策为
  `true,false,true`，重复组 groupId 相同，独立文档 groupId 为 null；
- 相同 session 和相同参数返回原 taskId；相同 session 改变参数返回
  `409 IDEMPOTENCY_CONFLICT`；新 session 指向已有目标最终返回
  `OUTPUT_CONFLICT`。

并发启动验证另外使用全新 database，同时运行两个 `prestart.ps1`，两个进程
均以 0 退出，最终 Alembic 版本为 `20260731_01` 且任务表存在。迁移入口使用
PostgreSQL advisory lock，避免 API 与 worker 首次启动时争抢创建版本表。
