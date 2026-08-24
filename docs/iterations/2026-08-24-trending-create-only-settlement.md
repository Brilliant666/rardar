# TRENDING-CREATE-ONLY-SETTLEMENT-01 — Linux create-only publication settlement

日期：2026-08-24
状态：Hotfix 实现；只有对应 PR 合并且最新 `main` Verify 恢复成功后才算完成

## 唯一目标

修复 PR #26 合并后在 Ubuntu Verify 暴露的 same-slot create-only 并发收尾竞态。Hotfix 只改变失败发布者读取并发赢家 target 的方式；append-only 路径、hard-link no-replace 发布、历史 capture 不可覆盖以及普通 stable-read/audit 的严格语义保持不变。

本轮不实现 24 小时增长 artifact、不开始 TopicEye POC、不接入 Scheduler，也不访问或部署 Production。

## 根因与旧 main 复现

`d29f1216fcaba5f5adcdee2288027422b4a3a1a9` 的发布者先 `os.link(temporary, target)`，完整读取 target 后再 `unlink(temporary)`。在 Linux 上，删除 temporary 会改变 target 共享 inode 的 link metadata/ctime。失败发布者收到 `FileExistsError` 后若两次完整 snapshot 跨过该 unlink，bytes、SHA-256 和 inode 均未改变，但 ctime 改变；`stable_read` 正确返回 retryable `concurrent_change`，调用方却永久归类为 `existing_capture_invalid`。

新增确定性两发布者测试，以 barrier 和事件精确排列：A hard-link 成功，B 得到 `FileExistsError`，B 完成 target 第一 snapshot，A 删除 hard-link source，然后 B 观察 `concurrent_change`。该测试在旧 main 稳定失败为 `existing_capture_invalid`，因此 `OLD_MAIN_REPRODUCED=YES`；Hotfix head 上 A 返回 `captured`、B 返回 `already_captured`。

## 修复合同

只有 `write_capture_create_only()` 的两条 same-slot 路径启用内部 settlement reader：

1. create 前发现 target 已存在；
2. `os.link()` 得到 `FileExistsError`。

普通 `load_capture()`、Store Audit 和 `pipeline/stable_read.py` 没有改变，默认仍为一次双 snapshot 严格读取。

Settlement 最多执行 4 次完整 stable-read，退避为 5/10/20ms，总等待预算 35ms。只有 `reason == concurrent_change` 且 `retryable == true` 才可能继续；并且前后必须仍是同一 device/inode/type/size/mtime，link count 必须下降，证明观察到的是同一不可变 inode 的 hard-link source release。随后仍完整验证 strict UTF-8 JSON、无重复 key、Schema、digest、captureId、scheduled path 和 policy。

以下情况不重试或不接受：unsafe type、symlink/reparse、unavailable/IO、替换 inode、同长度原地改写、文件消失、JSON/Schema/digest/identity/policy 错误。持续合法 hard-link metadata 变化达到上限返回 `capture_create_settlement_failed`；永久损坏继续返回 `existing_capture_invalid`。赢家的 `os.link → target validation → temporary unlink → directory fsync` 顺序没有调整。

## 行为与压力验证

回归测试覆盖：

- 原两发布者并发测试与精确 hard-link/unlink 时序；
- 已有损坏 JSON、digest mismatch、错误 captureId、symlink/path traversal；
- valid-byte delete/recreate 的 inode 替换；
- 恢复 mtime 后的同长度、同 inode、有效 digest 原地改写；
- 4 次 hard-link metadata 变化耗尽 35ms settlement budget；
- target 出现后消失；
- non-concurrent stable-read 错误不重试；
- 合法同 slot target 返回 `already_captured`，且 observer 不访问 GitHub；
- 通用 stable-read 套件继续验证 delete/recreate、同长度改写、symlink 和 digest mismatch 的严格默认行为。

Windows 和 exact-head Ubuntu Verify 均必须执行 500 轮双发布者压力测试。每轮唯一合法结果是一个 `captured` 和一个 `already_captured`；exceptions、partial files、temporary residuals 和 digest mismatch 必须全部为零。GitHub exact-head Verify 是 Ubuntu 24.04 合并门禁，合并后的 `main` push Verify 还必须恢复为 SUCCESS。

## 数据、安全与回滚

测试只使用临时 data 目录和 fake/local payload，不访问 GitHub、D1、Primary Runtime 或 Production，不改变 `data/current.json`、generation、Catalog 或正式 observation。Hotfix 不修改 Schema、store layout、candidate queries、26h carry-forward、UI、Scheduler、Manager、AI 或 TopicEye。

回滚只需回滚应用提交；它不要求数据迁移，也不得删除已经发布的历史 capture。若 exact-head 或 `main` Verify 未通过，保持 main recovery 阻塞并停止后续产品任务。
