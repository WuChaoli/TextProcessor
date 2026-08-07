# 137 结构化提取生产运行手册

## 部署边界

137 保持 TextProcessor API、Task Runner、Celery、PostgreSQL 和 Redis 的 systemd
部署。MinerU 是独立外部服务，本项目只消费其 HTTP API，不负责启动、升级或回滚。
Docling 以容器运行，只绑定回环地址或经批准的内网地址。真实凭据只写入生产运行时
环境文件，不写入 Git、命令行参数或验收报告。

## 基线与配置来源

修改前必须记录以下只读证据：

```bash
systemctl list-units --type=service --all | grep -E 'textprocessor|celery'
systemctl show <unit> -p FragmentPath -p DropInPaths -p EnvironmentFiles
systemctl status <unit> --no-pager
docker ps --format '{{.ID}}|{{.Names}}|{{.Image}}|{{.Status}}'
ss -lntp
```

先通过 `systemctl show` 和 `/proc/<pid>/environ` 确认实际消费结构化提取配置的
unit 和环境文件。不得凭 unit 名猜测配置路径，也不得输出 API key、数据库密码或完整
环境内容。

## Docling 上线

使用仓库固定的 `DOCLING_BASE_IMAGE` 和 `services/docling_service/Dockerfile` 构建
包装镜像。只启动 Docling 及其明确选定的 Redis 依赖，不执行会替换 API、Task Runner、
数据库、分类或 Data-Juicer 的全栈 `compose up`。API 端口绑定到 `127.0.0.1:5001`，
并设置独立的 `DOCLING_SERVE_API_KEY`。

上线最低检查包括：容器为 healthy、API 与 RQ worker 子进程存在、认证请求成功、
Docling 使用预期 Redis DB。健康检查只证明可接通，不替代真实格式验收。

## 开放生产格式

对已确认的环境文件先创建同目录时间戳备份，再设置：

```dotenv
EXTRACTION_WORKER__MINERU_BASE_URL=http://127.0.0.1:9000
EXTRACTION_WORKER__MINERU_API_KEY=
EXTRACTION_WORKER__DOCLING_BASE_URL=http://127.0.0.1:5001
EXTRACTION_WORKER__DOCLING_API_KEY=<runtime-secret>
EXTRACTION_WORKER__PRODUCTION_FORMATS=["text","markdown","json","xml","yaml","csv","tsv","pdf","image","pptx","xlsx","docx","html","epub"]
```

使用部署虚拟环境加载相同环境文件，且只回显处理器 host/port、profile 名和
`production_formats`，确认 Pydantic Settings 可解析后才重启。只重启已证明加载
`ExtractionWorkerSettings` 的 Task Runner/Celery unit；API 仅在进程环境证明确实消费
变更值时重启。重启后记录新 PID、active 状态和启动日志错误摘要。

## 上线后验收

先开放格式，再分别执行处理器直连与生产 API 验收：

- MinerU：PDF、PNG、JPG、PPTX；
- Docling：普通 DOCX、XLSX、HTML、EPUB；
- MinerU：复杂视觉 DOCX。

每项记录 task ID、最终状态、detected format、processor、路由理由、耗时、重试、
结果摘要、内容断言和 manifest。PNG/JPG 必须是独立任务；DOCX 必须覆盖两个路由。
报告不复制完整正文。测试失败保持格式开放，只报告失败阶段和建议，由用户决定是否
收紧。

## 观察与回滚

验收期间观察 CPU/GPU、内存、临时磁盘、Celery 积压、重试和 processor slot。
禁止通过删除任务记录、输入或已发布结果来处理失败。

只有获得用户明确授权后才收紧格式：恢复或修改已备份的 allowlist，先验证解析，再
重启实际消费者。Docling 回滚只停止本次部署的 Docling 并恢复 TextProcessor 环境
配置；MinerU 不属于回滚目标。任何停止操作前重新核对准确 unit、容器 ID 和绝对配置
路径，不执行 `down --volumes`。
