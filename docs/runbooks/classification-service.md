# Classification Service 运行手册

该服务只供 Compose 默认网络内的内部调用方访问，不映射宿主机端口，也不加入 Traefik 公网网络。生产环境必须挂载只读、不可变且 `production-approved` 的模型 release，并只暴露一张逻辑 GPU。

## 配置与启动

1. 从 `.env.example` 复制所需的 `CLASSIFICATION_*` 配置到受控 `.env`，令牌不得提交。
2. `CLASSIFICATION_MODEL_ROOT` 指向宿主机 release 根目录；`CLASSIFICATION_MODEL_RELEASE` 是该根目录下的 release 目录名。
3. 用 `tools/validate_release.py` 离线校验 release，并把 `manifest.json` 的 SHA256 写入配置。
4. 运行 `docker compose build classification-service` 和 `docker compose up -d classification-service`。
5. 在目标 GPU 主机运行 `powershell -File scripts/verify-classification-service.ps1`。

baseline `20260729T093134Z-321175f0` 仅为 `experimental`，禁止作为 production release。

## 健康、停机与故障

`/health/live` 只表示进程存活；`/health/ready` 仅在 release 校验、CUDA 检查、两个模型加载和 smoke 全部完成后返回 200。CUDA OOM 会立即取消 ready，并触发进程退出以交给容器重启策略处理。正常停机先停止新请求准入，拒绝排队请求，再等待正在运行的单线程推理完成。

日志和公开错误不得包含正文、内部令牌、宿主机绝对路径或堆栈。验证脚本会扫描常见敏感字段；人工排障也不得打印 `.env` 或 release 的宿主机路径。

## 验收边界

本地 CPU 只运行 fake 和静态测试。必须在授权 RTX 3090、真实 release 与 CUDA 环境中运行 real integration、容器 build/up 和验证脚本后，才能声明 GPU 部署验收通过。未运行时统一记录 `real_integration not run`。
