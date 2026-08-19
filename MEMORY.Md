# 运维记录

## 2026-08-13：137 服务器 GPU 1/2 服务清理

- 执行时间：2026-08-13 17:40 CST。
- 已确认并停止 TextProcessor 自有的 `textprocessor-classification.service`。该服务原运行于 GPU 1，停止后状态为 `inactive (dead)`，主进程 PID 为 `0`。
- GPU 1 显存占用从 19700 MiB 降至 18271 MiB，TextProcessor 分类推理进程已从 NVIDIA 计算进程列表消失。
- `textprocessor-classification-worker.service` 未使用 GPU，保持 `active (running)`，未停止。
- GPU 1 和 GPU 2 剩余约 18 GiB 的主要占用来自 `star` 用户的 `pt_data_worker` 进程，不属于 TextProcessor 服务，因此未停止或修改。
- GPU 0 上的 MinerU、VLLM 和 `VideoSummary_Extraction` 进程以及其他用户服务均未操作。
