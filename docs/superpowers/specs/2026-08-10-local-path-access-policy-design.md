# 本地路径访问能力策略设计

## 1. 背景与问题

当前结构化提取、Markdown 清洗、全局去重和文本分类分别通过输入或输出根目录配置限制调用方提供的本地路径。API 接收请求时校验路径是否位于白名单根目录，worker 在读取输入或发布结果时再次执行类似检查。

这种机制要求部署方提前枚举业务目录。137 服务器上的调用方使用 `/data/shineData/hub/txt/*` 时，即使目录存在且服务账号具备访问权限，只要 API 或 worker 的 roots 配置未覆盖该目录，请求仍会被拒绝。把 roots 扩大为 `/` 也不能可靠解决问题：API 与 worker 可能加载不同配置，worker 的 `/` 输出根还会包含内部 staging，触发重叠校验。

本次变更取消调用方本地业务路径的应用层根目录白名单，改为统一的访问能力检查。服务能否读写某个本地路径，以 API 和 worker 实际运行账号的文件系统权限及真实操作结果为准。

## 2. 目标

- 四项能力使用一致的本地路径访问语义；
- 调用方本地输入可位于任意目录，只要运行账号可读取且目标是现存普通文件；
- 调用方本地输出可位于任意目录，只要父目录已经存在且运行账号能够安全发布；
- API 在提交阶段快速预检，worker 在真实读取或发布前重新验证；
- 移除业务路径对 `*_INPUT_ROOTS`、`*_OUTPUT_ROOTS` 的依赖，不保留兼容开关或双模式；
- 保持 staging、远程资源策略、格式与大小限制、输出冲突、原子发布和错误脱敏边界；
- 在 137 的真实 systemd 运行账号和 sandbox 下完成正向、负向验收。

## 3. 非目标

- 不放开 HTTP host/CIDR、S3 bucket、协议、凭据、大小或超时限制；
- 不允许调用方指定、替换或扩大内部 staging 目录；
- 不自动创建调用方指定的输出父目录；
- 不改变目标文件默认不覆盖的语义；
- 不提升 API 或 worker 的 Linux 权限，不使用 root 绕过文件系统访问控制；
- 不改变处理器选择、算法、任务状态机或异步任务模式；
- 不把本地路径能力扩展为长期文档资产管理或任意远程资源访问能力。

## 4. 选定方案与安全边界

新增共享 `LocalPathAccessPolicy`，作为所有能力访问调用方本地业务路径的唯一应用策略。各业务模块保留格式、大小、协议、冲突和领域校验，但不再维护根目录归属判断。

策略只处理调用方业务路径：

- 输入检查验证绝对路径、真实打开能力和打开后文件类型；
- 输出检查验证绝对路径、既有父目录及实际安全发布能力；
- 路径是否允许不再由 roots 决定，而由运行账号权限与真实 I/O 结果决定；
- 目标已存在时仍按既有幂等恢复与冲突规则处理；
- staging 继续由服务端配置控制，不能通过请求参数改变，也不复用业务路径语义。

取消应用层 allowlist 后，生产安全边界转移到专用非 root 进程身份、Unix mode、ACL、挂载权限、容器挂载和 systemd sandbox。应用不负责模拟第二套目录授权系统，但仍负责安全打开、类型验证、大小限制、原子发布和错误脱敏。

## 5. 本地输入语义

本地输入必须满足：

1. 请求值是当前操作系统语义下的绝对路径；
2. worker 能以只读方式真实打开目标；
3. 打开后通过 `fstat` 确认文件描述符指向普通文件；
4. 打开后的真实大小不超过能力限制；
5. 后缀、内容类型和各能力格式规则继续生效。

不能只用 `Path.exists()`、`Path.is_file()` 或 `os.access()` 作为最终判断，因为检查和打开之间可能发生删除、替换、权限变化或符号链接变化。API 可以用快速预检提前返回明显错误，但 worker 必须重新打开，并以打开后的描述符状态为准。

不再判断解析路径是否属于输入 roots。符号链接按照操作系统实际解析结果访问；失效、循环、解析失败或权限不足均作为输入访问失败。目录、设备、socket、FIFO 等非普通文件一律拒绝，不能因它们可打开而进入处理器。

API 预检成功不保证 worker 随后成功。入队后发生路径、文件或权限变化时，worker 必须安全失败，不能跳过检查、自动切换 OSS 或提升权限。

## 6. 本地输出语义

本地输出必须满足：

1. `targetPath` 是绝对文件路径；
2. 父路径能够解析、已经存在且是目录；
3. API 在不创建最终业务产物的前提下完成快速预检；
4. worker 在目标目录实际创建独占临时文件，并以真实系统调用结果验证写权限；
5. 目标不存在，或者符合本任务已有摘要恢复条件；
6. 校验后的结果通过既有原子发布模型落盘，不暴露半成品。

服务不创建缺失的调用方父目录。API 不得留下探测文件；单独依赖 `os.access()` 也不能证明 worker 后续一定成功。权限、ACL、挂载和目录状态可能变化，因此 worker 的实际创建、链接、重命名或打开结果是最终依据。

取消输出 roots 不取消冲突和恢复校验。目标已存在且不能证明属于同一任务时继续返回 `OUTPUT_CONFLICT`，不得覆盖。目标目录内的临时文件必须使用不可预测名称和独占创建，清理范围必须是本次明确创建的临时文件，不能递归处理调用方目录。

## 7. API 与 Worker 双阶段检查

```text
API request
  -> absolute-path and obvious type preflight
  -> read preflight or output-parent preflight
  -> create task

Worker
  -> reopen local input
  -> fstat regular-file and size validation
  -> process into private staging
  -> create exclusive temporary in target directory
  -> atomic publish or stable access/conflict error
```

API 与 worker 使用同一个共享策略获得一致错误分类，但职责不同。API 负责尽早发现问题；worker 负责真实授权结果和数据安全。任何 API 预检结果都不能成为 worker 绕过真实 I/O 检查的凭据。

## 8. 四项能力接入

### 8.1 结构化提取

- API request policy 使用共享输入、输出访问检查；
- worker input resolver 在准备输入时重新打开并验证；
- publisher 以目标目录中的实际独占创建和原子发布为准；
- processor、私有 manifest、staging、恢复和不覆盖语义不变。

### 8.2 Markdown 清洗

- API request policy 改用共享本地路径策略；
- resolver 与 publisher 在真实 I/O 前重新检查；
- HTTP allowlist、Markdown pipeline 和 staging 防护保持不变。

### 8.3 全局去重

- API request policy 改用共享本地路径策略；
- input reader 与 publisher 在真实 I/O 前重新检查；
- HTTP、S3、输入 manifest 内容限制和 Data-Juicer adapter 边界不变。

### 8.4 文本分类

- 调用方只提供本地输入，不提供输出目标；
- input preparer 使用共享输入检查，不再检查 `CLASSIFICATION_INPUT_ROOTS`；
- `CLASSIFICATION_STAGING_ROOT` 仍由服务端控制；
- 分类服务调用、超时、结果契约和恢复保持不变。

## 9. 配置迁移

以下配置不再参与业务路径判断，并从 Settings、生产模板、Compose、systemd 环境说明、运行手册和测试夹具中移除：

```text
EXTRACTION_INPUT_ROOTS
EXTRACTION_OUTPUT_ROOTS
EXTRACTION_WORKER__OUTPUT_ROOTS
MARKDOWN_CLEANING_INPUT_ROOTS
MARKDOWN_CLEANING_OUTPUT_ROOTS
MARKDOWN_CLEANING_WORKER__OUTPUT_ROOTS
GLOBAL_DEDUP_INPUT_ROOTS
GLOBAL_DEDUP_WORKER__OUTPUT_ROOTS
CLASSIFICATION_INPUT_ROOTS
```

各能力 staging 配置继续保留。升级不提供 `LOCAL_PATH_ALLOWLIST_ENABLED` 等开关，也不保留 roots 为空、roots 为 `/` 或关闭检查等多套语义。生产 `.env` 中的旧变量在发布时清理，避免运维人员误认为仍生效。

## 10. 错误模型与兼容性

本地访问错误统一为：

- 相对路径或请求字段结构错误：现有请求校验错误；
- 输入不存在、不是普通文件、解析失败或不可读：`INPUT_ACCESS_FAILED`；
- 输出父目录不存在、不是目录、解析失败或不可写：`OUTPUT_ACCESS_FAILED`；
- 目标文件已存在且不符合恢复条件：`OUTPUT_CONFLICT`；
- API 预检后 worker I/O 失败：对应输入或输出访问错误；
- 格式、大小、HTTP、S3 和处理错误：保持各自现有错误码。

`INPUT_PATH_NOT_ALLOWED` 和 `OUTPUT_PATH_NOT_ALLOWED` 不再由本地路径分支生成，但枚举值保留一个兼容周期，以免历史任务或数据库记录反序列化失败。API 文档将其标记为历史错误；是否彻底删除由后续独立版本决定。

对外响应和默认日志不得包含未经脱敏的宿主机绝对路径、内部堆栈、凭据或文档内容。内部日志只记录 `request_id`、`task_id`、调用方标识和受控失败分类。

## 11. 生产安全门禁

实施和发布必须同时满足：

- API 与 worker 使用专用非 root 账号；
- 该账号不能读取生产密钥、系统配置和无关业务目录；
- 输入目录仅授予必要读权限，输出和 staging 仅授予必要写权限；
- 不为解决访问失败而给服务账号授予全盘权限；
- systemd 的 `ProtectSystem`、`ProtectHome`、`ReadOnlyPaths`、`ReadWritePaths`、`InaccessiblePaths` 或容器挂载不得被静默削弱；
- staging 清理只能作用于根据服务端 root 与 task ID 推导并验证的任务目录；
- 调用方路径不得进入递归删除、权限修改或目录创建操作；
- 项目规则中“不允许任意宿主机路径”的旧约束同步更新为本设计的运行账号能力模型。

若运行账号实际权限超出预期，必须通过 ACL、账号权限、systemd sandbox 或挂载策略收紧，不恢复应用层双模式 allowlist。

## 12. 测试范围

### 12.1 共享策略单元测试

- roots 之外的现存可读普通文件通过；
- 相对输入、缺失输入、目录、设备、FIFO、socket、不可读文件、失效或循环链接被拒绝；
- 打开后通过 `fstat` 识别替换后的非普通文件和真实大小；
- 任意既有可写父目录下的绝对目标通过输出预检；
- 相对输出、缺失父目录、父路径不是目录和不可写目录被拒绝；
- 预检不创建最终文件或残留探测文件；
- 目标存在由冲突策略处理，不被访问策略覆盖；
- 错误响应不泄露绝对路径和内部异常。

### 12.2 各能力契约与集成测试

- 三项输出能力接受原 roots 之外的可读输入与可写输出；
- 文本分类接受原 roots 之外的可读输入；
- API 预检后删除、替换输入或改变输出父目录状态，worker 返回稳定访问错误；
- API 与 worker 使用同一共享策略获得一致分类；
- 目标冲突、不同目标并发、重复消息、中断和摘要恢复不退化；
- HTTP host/CIDR、S3 bucket、协议、格式、大小和超时限制继续生效；
- 请求不能改变 staging，终态清理不触及调用方目录；
- 删除旧 roots 配置后 API 和 worker 正常启动。

### 12.3 真实环境测试

- 使用与生产一致的非 root 账号运行 API 和 worker；
- 从两个不同既有目录读取输入，并向两个不同既有可写目录发布；
- 覆盖无权限输入、无权限输出、缺失父目录和已有目标；
- 核验任务状态、最终文件、日志脱敏、重复投递和 staging 清理；
- 核验服务账号无法读取生产密钥及明确选定的无关敏感路径。

## 13. 文档与契约同步

实现时同步更新：

- 根 `AGENTS.md` 的本地路径约束；
- 四项能力相关架构说明；
- 137 systemd 运行手册和环境示例；
- `.env.example`、Compose 配置和测试夹具；
- API 中文字段、稳定错误说明和 Apifox 测试用例。

请求字段结构不改变。错误码映射以实现后的代码和 FastAPI OpenAPI 为权威同步 Apifox，不在设计文档中声称未经实现验证的 schema 已上线。

## 14. 生产发布与验收

1. 建立共享 `LocalPathAccessPolicy`，完成四项能力的 API 与 worker 接入；
2. 移除 roots 配置及重复归属实现，保留 staging 和远程资源策略；
3. 完成单元、契约、恢复和真实文件系统权限测试；
4. 更新项目规则、运行手册、环境配置和 Apifox；
5. 在 137 记录 unit、运行账号、group、sandbox、权限和发布前配置快照；
6. 备份将修改的生产环境及 unit 配置；
7. 运行配置解析和数据库迁移检查，滚动重启 API 与实际消费者；
8. 使用 `/data/shineData/hub/txt` 和另一处受控目录执行正向测试；
9. 执行不可读、不可写、缺失父目录和目标冲突负向测试；
10. 核验 PostgreSQL、最终文件、日志脱敏、远程 allowlist、重复投递和 staging 清理。

生产验收标准：四项能力不再因本地目录不属于 roots 而拒绝；运行账号可访问的本地输入和输出成功，不可访问路径稳定失败；远程资源、格式限制和 staging 边界不变；任务恢复、冲突和不覆盖语义通过；API 与 worker 均保持非 root 且无法访问明确验证的敏感路径。
