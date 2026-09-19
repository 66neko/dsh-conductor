# 变更记录

## 0.5.0 — 2026-09-19

- 新增 cancel_event、整个 run 的截止时间、独立清理预算和 workspace 重入保护。
- DSH 使用可取消的非阻塞管道，严格要求 turn/end 与 idle 同时完成，拒绝非法协议帧。
- 每轮使用私有 tmux socket、资源身份登记、CleanupReport 和幂等 cleanup_run/CLI cleanup。
- ConductorError 新增 code/phase/details/cleanup；清理失败时保留可信 exc.result。
- CLI 将 SIGINT/SIGTERM 转为取消并输出单个 JSON，超时退出码 124，信号取消为 130/143。
- 进度队列有界，回调与日志收尾遵循剩余预算。新增生命周期故障与真实 tmux 隔离测试。
- 恢复清理拒绝损坏的进程身份记录；日志写锁和 DSH 关闭阶段遵守各自截止时间。
- 兼容性变化及 Linux 资源核验边界见 [迁移说明](docs/lifecycle.md#从-041-迁移)。
