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

当前阶段不提供 Docker 运行方式。
