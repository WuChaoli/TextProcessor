# 137 本地路径访问策略生产验收报告

## 结论

2026-08-10 已在 137 部署取消应用层本地 roots allowlist 的版本。结构化提取、Markdown
清洗、全局去重和文本分类统一按 `textprocessor` 运行账号的 Linux 文件权限与 systemd
沙箱访问本地绝对路径。生产结构化提取已用真实 PDF 连续写入两个不同 `targetPath`，两项
任务均成功；输出目录未出现公开 `manifest.json`。

调用方给出的 `/data/shineData/hub/txt/1.pdf` 在验收时不存在，因此该路径返回稳定错误
`INPUT_ACCESS_FAILED`，不是 allowlist 拦截。目录中实际存在的 PDF 为
`/data/shineData/hub/txt/2086650861565562883.pdf`。

## 部署与权限证据

- 项目目录：`/shineData/text_processor`。
- 运行账号：专用非 root 账号 `textprocessor`。
- 四个应用 unit：`textprocessor-api.service`、`textprocessor-task-runner.service`、
  `textprocessor-classification-worker.service`、`textprocessor-markdown-worker.service`。
- 四个 unit 在验收时均为 `active (running)`，应用进程用户均为 `textprocessor`。
- systemd 约束包括 `ProtectSystem=full`、`ProtectHome=yes`、`NoNewPrivileges=true`、
  `PrivateTmp=true`、`UMask=0077`、`ReadOnlyPaths=/data /shineData/text_processor`、
  `InaccessiblePaths=/root`；写目录通过 `ReadWritePaths` 精确开放。
- 权限探针确认业务 PDF 可读、`/runtime/output` 可写、`/etc/shadow` 不可读。
- 旧 `*_INPUT_ROOTS`、`*_OUTPUT_ROOTS` 配置已从生产环境移除。

## 生产 API 验收

| 场景 | task ID | targetPath | 结果 |
|---|---|---|---|
| 不存在的原始路径 | 不创建任务 | `/runtime/output/1.md` | HTTP 400，`INPUT_ACCESS_FAILED` |
| 现存 PDF 首次提取 | `019fea36-1df4-723c-ac62-94e52847780c` | `/runtime/output/pdf-acb5d4e0c8.md` | `succeeded`，3498 bytes |
| 同一 PDF 更换输出路径 | `019fea36-bf9a-7708-8437-d4982ebff81d` | `/runtime/output/pdf-second-b6300db4fc.md` | `succeeded` |

第二次任务证明同一输入可在后续调用中使用不同 `targetPath`。验收后检查
`/runtime/output`，不存在 `manifest*.json`；manifest 只在任务私有 staging 中参与恢复，
终态清理，不与其他任务共享文件名，也不发布到输出目录。

## 回滚信息

- 部署前源码归档：`/shineData/text_processor-backup-20260810T053547Z.tgz`。
- 原 root 虚拟环境：`/shineData/text_processor/.venv.root-backup-20260810T054140Z`。
- API 与 Task Runner unit 备份后缀：`.bak-20260810T053455Z`。
- 分类与 Markdown worker unit 备份后缀：`.bak-20260810T054623Z`。

这些文件当前保留用于人工回滚，未自动删除。回滚前必须再次核对目标路径与 unit，且不得
删除任务记录、业务输入、已发布结果或数据卷。

## 验收边界

本次验收聚焦本地路径策略与 manifest 生命周期，真实 PDF 已通过生产链路。缺失路径的
失败是文件不存在的预期行为。本报告不把健康检查等同于全部格式的重新验收，也不记录
凭据、原文内容或完整环境变量。
