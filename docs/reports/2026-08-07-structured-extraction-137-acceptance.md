# 137 结构化提取生产格式验收报告

## 结论

2026-08-07 已在 137 完成 MinerU 接入和 Docling 上线，运行中的 Task Runner 已加载完整
production allowlist。处理器直连测试 MinerU 4/4、Docling 4/4 通过；生产 API 的八种
格式及 DOCX 双路由共 9/9 任务成功。所有格式继续保持开放，无收紧建议。

本报告只记录必要摘要，不含 API key、生产密码或业务文档正文。

> 历史行为说明：本报告记录的是 2026-08-07 版本，当时生产链路会在输出目录发布
> `manifest.json`。自私有 manifest 生命周期版本起，表格中的 manifest 证据仅作为历史
> 验收记录保留；新版本只发布目标 Markdown，并以 PostgreSQL 摘要和终态 staging 清理
> 作为验收依据。

## 部署与配置证据

- TextProcessor API 与 Task Runner 继续由 systemd 管理；最终均为 `active`。
- MinerU 保持独立容器 `mineru-api`，健康 API 为 `http://127.0.0.1:8001/health`，
  TextProcessor 不管理其生命周期。
- Docling 包装镜像 ID 为
  `sha256:4ba6333067d01badbded6a6d53d739bdc2d3c782bef8e90b5ac789a6ebf8979e`，容器
  `tp-docling` 只绑定 `127.0.0.1:5001`，组合健康检查为 `healthy`，重启次数为 0。
- Docling API 与 RQ worker 同容器运行，队列使用 `tp-source-redis:6379/1`；凭据文件
  `/shineData/text_processor/runtime/docling.env` 权限为 `600`。
- 生产环境备份为
  `/shineData/text_processor/.env.backup-structured-formats-20260807T083637Z`。
- 运行时回读 allowlist：
  `text,markdown,json,xml,yaml,csv,tsv,pdf,image,pptx,xlsx,docx,html,epub`。
- 最终 Task Runner PID 为 `2733469`；API 未消费本次 worker 配置，因此未重启。

## 处理器直连测试

在最终代码、最终配置与最终样本上重新一次性执行：

| 处理器 | 格式 | 结果 | 证据 |
|---|---|---:|---|
| MinerU | PDF、PNG、JPG、PPTX | 4/4 通过 | 提交、轮询、下载、UTF-8、内容、profile 与版本断言；57.98 s |
| Docling | DOCX、XLSX、HTML、EPUB | 4/4 通过 | 提交、轮询、下载、UTF-8、内容与 profile 断言；20.31 s |

PNG 与 JPG 为独立用例。PNG 原验收词 `NVIDIA-SMI` 被 OCR 轻微误识别，但实际结果稳定
包含 `NVIDIA H800`，据此将断言改为可重复的可见文本。EPUB 初始合成包不符合 EPUB
ZIP 顺序约束，且适配器使用通用 MIME；修正为首个、不压缩的 `mimetype`，并提交
`application/epub+zip` 后通过。两项均属于验收资产/协议修正，未收紧格式。

## 生产 API 逐格式结果

最终验收 session 为 `prod-format-smoke-1786095825`。每项 `attempts=1`，最终状态均为
`succeeded`，内容断言与 `manifest.json` 一致性断言均通过。

| 格式 | task ID | detected format | processor | 路由理由 | 耗时(s) | Markdown SHA-256 | manifest SHA-256 |
|---|---|---|---|---|---:|---|---|
| PDF | `019fdb9b-74e2-7335-9e44-caeeb563c711` | pdf | mineru | `fixed_route=pdf` | 37.755 | `1923fe4f8d5e4c1add3637b08bee99ef8e6efb010373b4c6f0305a498920020a` | `c9c036a657bc764b66176dcef2821884a3f566485e073864143c3b6733038ece` |
| PNG | `019fdb9b-752b-747b-bd72-fcb9b9d73abf` | image | mineru | `fixed_route=image` | 11.093 | `4b82175f89419afb3b04dd7a6e47c8f403ca18256aa68a06fe6a0026f9542c01` | `3d32fb845dff923290569cf0b5da714847c8f741ac3c57870bd727cfe8a6111c` |
| JPG | `019fdb9b-7553-7487-b77a-ded6333a9cf4` | image | mineru | `fixed_route=image` | 5.165 | `c501365c548f7d2e5d258d9b397f69aa8d3a866d00e1beec302e4ce4a2875aa6` | `07710b6750323cceb4e5f873b231afb5aa359dd3fe2e0ce4fa96068a0d0e9f32` |
| PPTX | `019fdb9b-757e-764c-9a78-d8d3ded2b3c0` | pptx | mineru | `fixed_route=pptx` | 5.294 | `5c587c60ce267750c395381924b1dd82123b95dec3b50ac5e30fea1a34ebd4ba` | `fa0f05be2dc8000cbb1ecc745821d6e7010e3e6f5c9f6402f4a4128509c1e98d` |
| XLSX | `019fdb9b-75a5-70cf-9780-c4ff6193851b` | xlsx | docling | `fixed_route=xlsx` | 5.343 | `74d3c065d7467bf0548b1f80b5ba8abb0abd89a68f44f7a82abbc114454f143e` | `b74a622e798662b3a0e0fd02335dbdac9f0b71bb9391cbe49413a13f354c2249` |
| DOCX 普通 | `019fdb9b-75cd-7017-b1bc-3c09b615f079` | docx | docling | `ordinary_docx` | 5.372 | `004be6469e8aca39db4c9a1c2ff121356c144ffa6c14504b8afc6a2537bdf986` | `ac243168a4df57f628f0c359b0f1a0e738d5c0bebd3df7d459540f8125b2ceb8` |
| HTML | `019fdb9b-75f7-717c-9b38-8d0b2df04efd` | html | docling | `fixed_route=html` | 5.199 | `0e3a503c8766432e757cbd7286842b6902769627b5a6f6758b76f8f8d6efbbdd` | `cea7a2323065ee9e7470ca290bf82436896acf1cb7df200118e5d477c8f66b6c` |
| EPUB | `019fdb9b-7621-7114-953b-def21cfde8bb` | epub | docling | `fixed_route=epub` | 5.281 | `2554e0157eeec5a38f55184cf3f2ce3d3b33e0bf52c86ef836daf90d92b76b22` | `01ec491164981ebca1c347b99b0cf608a93a6d2b327588075c7028e87d5d15ae` |
| DOCX 复杂 | `019fdb9b-7646-77c5-9599-8d5f4368f96b` | docx | mineru | drawings、anchored objects、text boxes、columns | 5.157 | `ffe52a4f8ffd1ea8152171f62b1654f4fa2f21504f0b3643b955922852abc0d7` | `c7e787ea11455515ff5e2b5d926c5fec7e761cbbee6faf4ddf03c48c0e158208` |

每个任务使用独立输出目录，目标 Markdown 与同目录 `manifest.json` 均非空。manifest
逐项核验 task ID、detected format、输入/输出摘要、processor/version/profile、路由理由和
发布路径；其 Markdown 摘要与 API 返回及实际文件三方一致。

## 资源与可靠性观测

- Celery broker DB 0 队列长度为 0；Docling RQ DB 1 `convert` 队列长度为 0。
- PostgreSQL 中最终 session 的 9 项任务均为 `attempt_count=1`；processor slot 数为 0，
  表示任务结束后无泄漏占槽。
- 采样时 MinerU 为 CPU 1.87%、内存 64.57 GiB；Docling 为 CPU 0.42%、内存
  1.067 GiB。该数据为验收后的有界时点采样，不声称代表峰值。
- GPU 采样时 5 张 RTX 3090 利用率均为 0%；显存分别为 16365、1236、27、27、27 MiB。
- API 内存约 142 MiB，Task Runner 内存约 340 MiB，两个 unit 均为 `active`。
- `/shineData` 可用 4,369,078,091,776 bytes，使用率 43%；结构化提取 staging 为
  6,756,653 bytes，本次验收输出合计 424,011 bytes。

## 失败分级与开放建议

最终验收无连接、提交、轮询、处理、下载、内容、发布或系统阶段失败。早期 PNG 与 EPUB
问题分别归类为内容断言选择和无效 fixture/提交 MIME，不是生产处理器不可用。当前保持
全部目标格式开放；无需收紧。后续若观察到真实业务样本质量问题，应保留失败任务证据，
由用户决定具体格式是否收紧，不执行自动 allowlist 变更。
