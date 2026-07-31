# 全局去重 API 实现计划

**Goal:** 实现已确认的异步全局去重 POST/GET 接口，并以 PostgreSQL 幂等、调用方隔离和最小 Celery 消息接入已完成的 worker。

**Architecture:** route 仅做鉴权和协议映射；service 管理幂等、状态转换和入队失败；repository 使用 PostgreSQL advisory lock 与唯一约束收敛并发；request policy 规范化并限制输入输出地址。

---

### Task 1: API schema、稳定错误与路径策略

- [x] 先写请求/响应 schema 与路径策略失败测试。
- [x] 实现 camelCase schema、长度和 `.json` 后缀约束。
- [x] 实现本地/file、HTTP 输入及首版受控输出策略。
- [x] 跑 focused pytest、Ruff、Mypy、Pyright、Ty。

### Task 2: PostgreSQL 幂等 repository 与 service

- [x] 先写真实 PostgreSQL 幂等、隔离、冲突和入队失败测试。
- [x] 实现 caller + session advisory lock、fingerprint 和 create-or-get。
- [x] 实现 pending → queued、最小消息投递和失败终态。
- [x] 跑 repository/service gates。

### Task 3: FastAPI POST/GET

- [x] 先写鉴权、202、409、503、404、进度及结果互斥测试。
- [x] 实现 routes、dispatcher 和 API router 注册。
- [x] 确认 GET 不返回结果正文且越权与不存在统一 404。
- [x] 跑 API 与 backend 全量门禁并提交。

### Task 4: 三模块真实合并测试

- [x] 源码启动 PostgreSQL、Redis、Data-Juicer API/worker、TextProcessor API/worker。
- [x] 通过 HTTP 创建任务并轮询成功，验证结果文件四字段不变量。
- [x] 验证幂等重复提交、冲突和禁止覆盖。
- [x] 停止临时进程并记录可复现证据。
