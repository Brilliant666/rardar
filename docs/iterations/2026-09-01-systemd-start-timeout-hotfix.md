# Systemd startup timeout hotfix

## 目标

让版本化 systemd unit 为只读部署预检提供足够且有界的启动窗口，删除 Production 对临时恢复 drop-in 的长期依赖。

## 根因证据

2026-09-01 对健康 Production release 和正式数据执行的只读分段审计耗时 94.389 秒：generation 的 manifest/hash、Schema 与跨文件 Audit 为 88.172 秒，release 校验为 6.162 秒，D1 稳定副本与完整性检查合计 0.042 秒，其余阶段低于 0.01 秒。检查没有网络请求、依赖安装或无界轮询；这是当前数据规模下的正常验证工作，不是挂死。systemd 默认约 90 秒的启动时限会在合法预检完成前终止 `ExecStartPre`，并使新 release 和旧 release 回滚都无法启动。

## 修复

- `deploy/systemd/rardar.service` 显式设置 `TimeoutStartSec=5min`。
- deployment CLI 在受监督子进程中运行，内部墙钟上限为 4 分钟。
- 内部超时返回 `deployment_preflight_timeout` 并保持非零退出；systemd 因而不会执行 Manager 的 `ExecStart`。
- CI 继续执行 `systemd-analyze verify`，并验证 unit、Git commit 与 release artifact 中 unit 字节一致。

## 安全与回滚

修复不改变 generation、D1、Scheduler、Discover、Retention、Nginx 或 Public Edge。预检仍只读且 fail closed。代码回滚切回上一 exact release；若 Production 暂时需要恢复 drop-in，只能把它作为诊断期保护，版本化 unit 验证成功后必须删除并再次受控重启。

## 非目标

- 不调整 Discover 排序或激活 Production Discover。
- 不手工触发 Observation、Refresh、Explosion 或 Discover。
- 不修改数据契约、D1 业务事实、Nginx 或 Public Edge。

## 验证

本轮要求完整 `npm run verify`、Linux `systemd-analyze verify`、timeout 失败路径、release artifact unit 字节一致性，以及 Production 两阶段 cutover。生产运行事实只在 exact artifact 合并、构建并通过部署门禁后成立。
