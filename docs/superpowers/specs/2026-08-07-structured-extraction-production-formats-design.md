# 结构化提取生产格式上线设计

## 1. 目标

在 137 服务器保持 TextProcessor API、Task Runner、Celery、PostgreSQL 和 Redis 现有 systemd 部署不变，接入已经独立运行的 MinerU 外部服务，在 137 新部署 Docling，并开放以下生产输入格式：

- MinerU：PDF、PNG、JPG、PPTX；
- Docling：XLSX、HTML、EPUB；
- DOCX：由现有结构预检确定性选择 Docling 或 MinerU；
- 本机直通格式继续支持 text、markdown、json、xml、yaml、csv、tsv。

本次不支持旧版 DOC、旧版 PPT、WPS、ET、DPS 和 OFD，不迁移现有 API/Worker 到 Compose，也不管理 MinerU 的生命周期。

## 2. 上线策略

采用“先接通上线并开放格式，随后测试，由用户决定是否收紧”的顺序。逐格式真实 smoke 不是开放前门禁，测试失败也不得自动移除格式或停止服务。

上线前仅保留最低启动门槛：

1. MinerU 地址可达且健康协议可用；
2. Docling 进程已启动且健康、认证可用；
3. TextProcessor 配置可解析，相关进程重启成功；
4. 不在日志、Git 或命令输出中暴露凭据。

完成上述检查后，生产 allowlist 设置为：

```text
text,markdown,json,xml,yaml,csv,tsv,pdf,image,pptx,xlsx,docx,html,epub
```

`image` 是内部统一格式值，对外验收必须分别覆盖 PNG 和 JPG。

## 3. 部署架构

```text
调用方
  -> TextProcessor API (systemd)
  -> PostgreSQL 任务记录
  -> Redis/Celery
  -> Task Runner / Celery Worker (systemd)
       -> PDF/PNG/JPG/PPTX -> MinerU 外部 API
       -> XLSX/HTML/EPUB -> Docling API (137 Docker)
       -> DOCX -> OfficeDocumentInspector
                    -> 普通文档 -> Docling
                    -> 复杂视觉文档 -> MinerU
  -> Markdown 规范化和结果校验
  -> 原子发布 Markdown；manifest 仅保留在非终态任务的私有 staging
```

MinerU 作为 TextProcessor 之外的既有服务，只消费其 API，不加入 TextProcessor Compose、启停脚本或回滚动作。Docling 使用仓库固定的镜像定义在 137 以容器方式部署，只允许本机或受控内网访问。处理器地址、认证、超时和 profile 通过 137 的 systemd 环境配置注入，不提交真实凭据。

## 4. 样本与内容断言

使用 `assets/` 中已有的 PDF、PNG、PPTX、XLSX 和 DOCX。新增不含敏感信息的最小合成 JPG、HTML、EPUB 和复杂视觉 DOCX fixture。合成样本必须包含稳定、唯一、可自动断言的目标内容。

| 格式 | 样本 | 预期处理器 | 最低内容断言 |
|---|---|---|---|
| PDF | 现有 PDF | MinerU | 标题及至少一个预选关键短语 |
| PNG | 现有 PNG | MinerU | OCR 结果非空并命中预选文字 |
| JPG | 合成图片 | MinerU | OCR 结果命中固定文字 |
| PPTX | 现有 PPTX | MinerU | 标题和至少一页正文 |
| XLSX | 现有 XLSX | Docling | 工作表表头及关键单元格 |
| DOCX 普通 | 现有 DOCX | Docling | 标题、段落或列表 |
| DOCX 复杂 | 合成 DOCX | MinerU | MinerU 路由理由及固定视觉文字 |
| HTML | 合成 HTML | Docling | 标题、段落、列表和表格值 |
| EPUB | 合成 EPUB | Docling | 元数据、章节标题和正文短语 |

现有业务样本只用于授权的生产验收，不提交到新的 Git 变更中。测试输出和报告只记录必要摘要，不复制完整正文。

## 5. 验证层次

### 5.1 处理器直连

MinerU 分别执行 PDF、PNG、JPG、PPTX 的提交、轮询、结果下载和内容断言。Docling 分别执行 DOCX、XLSX、HTML、EPUB 的提交、轮询、结果下载和内容断言。PNG 与 JPG 必须是两个独立用例，不能以单个 `image` 用例替代。

处理器返回成功但结果为空、乱码、缺少预设目标内容或协议字段不合法时，验收仍为失败。

### 5.2 TextProcessor 生产接口

每种格式通过生产结构化提取 POST 接口创建独立任务，再通过 GET 轮询。每项记录：

- task ID 与最终状态；
- 实际 detected format、processor 和路由理由；
- 处理耗时与重试次数；
- `targetPath`、最终 Markdown 摘要和内容断言结果；
- PostgreSQL 结果摘要、processor/profile 和发布信息；
- CPU、GPU、内存、临时磁盘、Celery 队列和 processor slot 的必要观测。

DOCX 必须覆盖普通 Docling 分支和复杂视觉 MinerU 分支。业务层结果必须为 `succeeded`，且结果经过原子发布；只验证外部处理器成功不足以证明生产链路完成。

## 6. 上线步骤和变更边界

1. 记录 137 当前进程、unit、端口、任务队列、配置来源和发布版本；对即将修改的 systemd 环境配置建立可恢复备份。
2. 核对 MinerU 外部 URL、认证和实际协议，只读验证健康和 API 可达性。
3. 制作缺失 fixture，补齐逐格式测试参数和内容断言。
4. 使用仓库固定镜像在 137 部署 Docling，验证健康、认证、持久化和重启后的可用性。
5. 写入 MinerU/Docling 配置和完整 production allowlist，执行配置解析检查。
6. 只重启实际加载结构化提取 worker 配置的 Task Runner/Celery 进程；API 仅在确认其消费相关配置时重启。
7. 确认生产接口不再对目标格式返回 allowlist 错误，随后执行处理器直连和生产端到端测试。
8. 输出逐格式验收报告并保持当前开放状态，等待用户决定是否收紧。

仓库变更聚焦于 `.env.example`、MinerU/Docling smoke 脚本、结构化提取 real integration 测试和 runbook。137 的真实密钥、服务 token 和生产密码不得进入仓库。

## 7. 失败处理与回滚

测试失败只记录事实、影响范围和建议，不自动收紧 allowlist。每个失败必须区分连接、提交、轮询、处理、结果下载、内容质量、发布和系统错误。

只有用户明确授权后才执行格式收紧。收紧时从 production allowlist 移除指定格式，验证配置后重启相关 worker；不得删除 PostgreSQL 任务记录、用户输入或已发布结果。

Docling 服务回滚仅停止本次部署的 Docling 并恢复对应 TextProcessor 环境配置。MinerU 是独立外部服务，不属于本次回滚目标。执行任何回滚前必须核对准确 unit、容器和配置文件路径。

## 8. 完成标准

- MinerU 外部 API 已接入，Docling 已在 137 上线并可达；
- 完整 production allowlist 已由运行中的 worker 加载；
- 生产接口开始接受 PDF、PNG、JPG、PPTX、XLSX、DOCX、HTML 和 EPUB；
- 八种格式以及 DOCX 双路由均有独立任务、处理器、耗时和结果证据；
- 最终 Markdown 非空且命中预设内容，数据库结果摘要与实际文件一致；
- 验收报告列出每种格式的结果、资源数据、失败阶段和保持开放或建议收紧意见；
- 测试失败时保持开放，是否收紧由用户决定；
- 未泄露凭据、完整业务正文或其他敏感信息。
