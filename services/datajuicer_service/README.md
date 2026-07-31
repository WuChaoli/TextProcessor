# Data-Juicer Service

内部异步数据处理服务。首版从源码运行，固定使用 Data-Juicer v1.5.4。

## 环境

- Python 3.11
- PostgreSQL
- Redis
- Data-Juicer submodule commit `7061da6ad06287aa0305eda162429b34361a56a3`

初始化：

```powershell
git submodule update --init --recursive
uv sync --project services/datajuicer_service --locked
```

官方 wheel 用于安装锁定依赖与包元数据；源码运行时必须把
`services/datajuicer_service/vendor/data-juicer` 放在 `PYTHONPATH` 首位。
启动脚本和兼容性检查会验证实际导入路径与固定 commit。

## 源码启动

根据 `.env.example` 在当前 PowerShell 会话设置 `DATAJUICER_*` 环境变量。
PostgreSQL 和 Redis 可以复用已有物理实例，但 Data-Juicer 必须使用独立
database/user、Redis logical DB 和 `datajuicer.jobs` queue。

先执行完整前置检查和 migration：

```powershell
& services/datajuicer_service/scripts/prestart.ps1
```

分别在三个终端启动：

```powershell
& services/datajuicer_service/scripts/run_api.ps1
& services/datajuicer_service/scripts/run_worker.ps1
& services/datajuicer_service/scripts/run_beat.ps1
```

API 默认监听 `127.0.0.1:8091`。`GET /health` 只表示进程存活，
`GET /ready` 会查询 PostgreSQL。

脚本会拒绝非 Python 3.11、错误的 Data-Juicer commit、过期 lock 或失败
migration；脚本不会启动或构建 Docker。

当前阶段不提供 Docker 运行方式。
