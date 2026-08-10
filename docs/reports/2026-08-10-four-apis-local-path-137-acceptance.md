# 137 四接口本地路径访问验收报告

## 结论

2026-08-10 在 137 生产环境通过真实 API 鉴权、POST 提交和 GET 轮询，完成结构化提取、
Markdown 清洗、全局去重、文本分类四个接口的本地路径访问测试。四个接口均有一次任务
到达 `succeeded`；结构化提取、Markdown 清洗和全局去重的目标产物真实存在，文本分类
返回真实分类结果。

本地 roots allowlist 未拦截任何一次请求。不过验收同时发现：全局去重当前通过硬链接
发布本地结果，staging 与 `targetPath` 位于不同文件系统时会以 `OUTPUT_WRITE_FAILED`
失败。因此“本地路径访问已开放”成立，但全局去重输出尚不能无条件跨文件系统发布。

## 成功验收结果

| 接口 | 本地输入 | 本地输出 | task ID | 终态 | 产物或结果 |
|---|---|---|---|---|---|
| 结构化提取 | `/data/shineData/hub/txt/2086650861565562883.pdf` | `/runtime/output/local-path-api-e2e-20260810T060154Z-10f5b4bb/extracted.md` | `019fea43-6a70-70fe-a695-fa85754d0b70` | `succeeded` | MinerU 3.4.0，PDF，3498 bytes |
| Markdown 清洗 | `/runtime/output/local-path-api-retest-20260810T060355Z-c3b52578/simple-input.md` | `/runtime/output/local-path-api-retest-20260810T060355Z-c3b52578/cleaned.md` | `019fea45-440f-77ce-90b4-9bc76822ff82` | `succeeded` | 产物存在，53 bytes |
| 全局去重 | 清单位于 `/runtime/output/local-path-api-retest-20260810T060355Z-c3b52578/dedup-input.json`，清单引用 `/data/shineData/hub/txt/1.txt`、`2.txt` | `/shineData/text_processor/runtime/local-path-api-retest-20260810T060355Z-c3b52578/deduplicated.json` | `019fea45-5bd3-7677-8369-4639231faeda` | `succeeded` | 2/2 文档处理完成，186 bytes |
| 文本分类 | `/data/shineData/hub/txt/1.txt` | 无文件输出 | `019fea43-6b26-7611-811e-9ec67da57891` | `succeeded` | 返回标签、置信度和 release ID |

所有成功任务的 POST 均返回 HTTP 202，终态 GET 均返回 HTTP 200。结构化提取测试目录中
未出现公开 `manifest*.json`。

## 补充测试与发现

### Markdown 既有文件

使用 `/data/shineData/hub/txt/2.md` 提交的任务
`019fea43-6ab8-772b-9e53-398d0d1288db` 成功入队并进入清洗阶段，最终以
`MARKDOWN_NORMALIZATION_FAILED` 失败。该结果证明本地文件已通过访问策略并被 worker
读取；失败发生于文档标准化，不是路径白名单或文件权限问题。改用最小规范 Markdown
后任务成功。

### 全局去重跨文件系统发布

任务 `019fea43-6af3-7269-a9f5-34e40ccb7779` 已读取本地清单及两份 `/data` 文档，进度达到
2/2、40%，外部 Data-Juicer 作业也正常完成，但发布到
`/runtime/output/local-path-api-e2e-20260810T060154Z-10f5b4bb/deduplicated.json` 时返回
`OUTPUT_WRITE_FAILED`。实现使用 `os.link(prepared.path, local_target)`；当 staging 与目标
位于不同文件系统时，Linux 硬链接不能完成发布。目标改到与 staging 同文件系统的
`/shineData/text_processor/runtime/...` 后成功。

该问题应作为全局去重 publisher 的跨文件系统发布缺陷处理，不能通过重新增加 roots
allowlist、提升为 root 或扩大 systemd 文件权限解决。

## 验收数据位置

- 首轮公共测试目录：`/runtime/output/local-path-api-e2e-20260810T060154Z-10f5b4bb`。
- 复测公共目录：`/runtime/output/local-path-api-retest-20260810T060355Z-c3b52578`。
- 全局去重同文件系统产物目录：
  `/shineData/text_processor/runtime/local-path-api-retest-20260810T060355Z-c3b52578`。

上述目录保留用于核验。本报告不包含认证令牌、生产密码、原始正文或完整环境变量。
