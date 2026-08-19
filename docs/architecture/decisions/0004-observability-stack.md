# ADR-0004：服务可观测性与性能诊断栈

- 状态：已接受
- 日期：2026-08-06

## 背景

TextProcessor 由 API、Celery worker、PostgreSQL、Redis 和独立能力服务组成。仅依赖应用日志无法可靠关联请求、后台任务、外部调用与资源瓶颈；同时，文本内容、文件 URI、凭据和宿主机路径具有敏感性，不能为了观测而扩大数据暴露。

## 决策

1. 服务使用 structlog 生成结构化日志。日志字段至少支持 `service`、`environment`、`request_id`、`task_id`、调用方标识、事件名、稳定错误码和耗时；默认不记录原始文本、文档正文、凭据、完整 URI、内部堆栈或宿主机绝对路径。

2. FastAPI 指标优先通过 `prometheus-fastapi-instrumentator` 暴露 Prometheus 格式端点。Celery worker 和独立能力服务使用兼容的 Prometheus 指标，避免高基数标签；`request_id`、`task_id`、文件名和原始路径不得作为指标标签。

3. OpenTelemetry 是分布式链路追踪标准。HTTP、Celery 和外部服务调用传播统一 trace context，并在日志中记录可关联的 trace/span 标识。采样、导出端点和凭据由部署配置控制。

4. Loki 汇聚结构化日志，Grafana 统一展示指标、日志和链路关联视图。Prometheus 与 OpenTelemetry Collector/后端的实际部署拓扑由部署设计确定；Grafana 不作为业务状态或审计记录的权威来源。

5. memray 和 py-spy 是按需性能诊断工具，不常驻生产请求路径。memray 用于受控环境的 Python 内存分配分析；py-spy 用于采样 CPU 栈。生产使用必须经过运维授权，并避免采集敏感载荷。

6. 可观测性分阶段启用：先定义事件、字段和指标语义，再接入采集和存储，最后建立告警与仪表盘。仅安装库或启动 Grafana 不构成可观测性完成证据。

## 结果

- PostgreSQL 仍是任务状态的权威来源；指标、日志和追踪用于诊断，不驱动业务状态转换。
- 三类信号共享关联标识和服务命名，但分别控制保留期、采样率和访问权限。
- 引入上述组件需要容量、保留期、脱敏、鉴权和故障降级设计；本文不宣称它们当前已经部署。

## 关联标准

- [可观测性标准](../standards/observability.md)
- [工程质量门禁](../standards/quality-gates.md)

