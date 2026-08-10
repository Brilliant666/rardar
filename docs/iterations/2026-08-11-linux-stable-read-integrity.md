# 2026-08-11 Linux Stable Read Integrity

## 唯一目标

`PROD-DEPLOY-01` 在 Ubuntu 24.04 exact release
`283321186f75d3d54e436d68dc1c6c55bab91fa7` 的完整 Verify 中暴露了
`test_safe_reader_detects_same_length_in_place_mutation` 不稳定：50 次运行有 21 次失败。
本轮只建立跨 Windows/Linux 的内容级 stable-read 契约并修复测试同步，不继续部署，
不修改 Primary data/D1，不执行 refresh，也不修改服务器上的旧 release。

分支为 `fix/linux-stable-read-integrity`，基线为 PR #15 的 Squash merge 提交
`283321186f75d3d54e436d68dc1c6c55bab91fa7`；P1-6C2 继续 deferred。

## Root cause

旧 resolver reader 只做一次完整内容读取。它在读取前后比较 `dev`、`ino`、
`mode`、`size`、`mtime_ns` 和 `ctime_ns`，但 Linux 文件系统可以让同 inode、同长度的
原地覆盖落在无法由这些时间字段可靠区分的观察窗口内。元数据只能证明路径、类型和
对象身份边界，不能证明内容没有变化。

旧测试也不是有效的内容快照竞态证明：它在第一次 `fstat` 返回旧 metadata 后立即覆盖
文件，写入可能在第一次内容读取之前已经完成。旧 reader 因调度和时间戳粒度不同而偶尔
检测、偶尔接受；测试没有保证 mutation 发生在两份完整内容快照之间。

在未修改的旧 main 上，用 `threading.Event` 在一次完整旧 snapshot 返回后才允许 writer
对同 inode 做 `AAAA -> BBBB`、flush/fsync 的确定性 harness，Windows 与目标 Ubuntu
均得到 `10/10` 漏检，证明 production primitive 和旧测试各自都有问题。

## Reader audit matrix

| reader | protected object | existing integrity mechanism | metadata-only? | expected digest | concurrent mutation semantics |
| --- | --- | --- | --- | --- | --- |
| `pipeline.stable_read` | no-follow regular file | 两次独立 FD 完整读取、每次 lstat/fstat 边界、bytes + SHA-256 一致 | 否 | 可选 | immutable 默认一次尝试；只有调用者显式选择时才对 `concurrent_change` 做很小的 bounded retry |
| generation current reader | mutable `current.json` | strict JSON、pointer Schema、stable read | 旧实现是单次 pathname read | 无 | 最多 3 次；只返回完整旧 pointer 或完整新 pointer，持续变化则失败 |
| generation manifest reader | immutable `manifest.json` | pointer `manifestSha256`、strict manifest contract | 旧实现把 hash 与 parse 分成两次 pathname read | 有 | 同一 stable bytes 同时做 SHA、parse、validate；变化或 digest mismatch fail closed |
| ready generation inventory | manifest 内全部 artifact | manifest `hashes[artifact]`、Schema、Audit | 旧 hash 是一次 stream read | 有 | 每个 artifact 对同一 stable bytes 直接核对 expected SHA；Schema/Audit 后再完整确认 inventory |
| resolver/migration/adoption readers | evidence、flat artifact、quarantine、archive、detached artifact | 路径/类型/identity、显式 SHA、Schema、prepared/resolved audit | resolver 旧 reader 的内容稳定性只靠 metadata | 视流程而定 | immutable 默认不 retry；同长度改写、换 inode、链接替换均失败 |
| Historical Identity Catalog | retained/current `catalog/latest.json` | ready manifest digest + identity bundle guards | 旧实现单次 `read_bytes` 后 hash | 有 | stable bytes 直接核对 manifest digest，再 parse/validate |
| deployment SQLite snapshot | SQLite main、现有 WAL/journal | source before/copy/after digest、scratch recovery/quick_check | 旧单次 `_file_digest` 内容与 metadata 混合 | 无 | 每个成员先做双 snapshot；最多 3 次明确变化重试，最终不稳定则阻止 preflight |
| deployment release validation | required release regular files | exact release/path/type/no-symlink、build/Verify、systemd read-only boundary | 原先只检查路径/type | 无 | required file 必须完成双 snapshot；并发内容变化返回 `release_file_unstable` |
| Node published bundle loader | pointer、manifest、ready artifacts | 同一 Buffer 计算 manifest/expected SHA 后 parse；host bridge 单请求只解析一个 generation | 否（ready bytes 有 digest） | manifest/artifact 有 | current 由原子 replace 发布；ready bytes 若读取中变化无法匹配 expected digest；本轮不重写 Node loader |

## 新 stable-read 契约

共享 primitive 位于 `pipeline/stable_read.py`：

1. `lstat` leaf，拒绝 symlink、Windows reparse point 和非 regular file；
2. 使用 read-only FD，平台支持时追加 `O_NOFOLLOW`；
3. `fstat -> 完整 FD read -> fstat`，并把打开对象重新绑定到 pathname identity；
4. 关闭后再次 `lstat`；
5. 重新打开同一 canonical path，重复完整 snapshot；
6. 只有两份 bytes、SHA-256 和允许的文件身份全部一致才成功；
7. 调用者提供 expected SHA 时，同一份返回 bytes 的 digest 必须精确匹配；
8. retry 只处理显式 `concurrent_change`，次数由调用者固定且很小；unsafe type、
   unavailable 和 digest mismatch 不重试。

metadata 继续作为 no-follow/type/object replacement 的额外 reject signal，不再充当内容
未变的最终证明。测试只 patch 独立的低层 snapshot function，在 snapshot A 完成后通过
Event 放行 writer；production API 没有 debug hook 或 test bypass。

## 行为测试

新增或重写的门禁覆盖：

- 同 inode、同长度、不同内容必须失败；
- 同长度改写后主动恢复旧 mtime 仍必须失败；
- unchanged content 返回 bytes 与 SHA；
- atomic replace 在显式 bounded retry 下只返回完整新旧版本之一；
- symlink swap fail closed；
- delete/recreate 默认 fail closed；
- expected SHA mismatch fail closed；
- ready manifest artifact 在 snapshot A/B 间被同长度篡改返回 `integrity_mismatch`；
- resolver 的原有 file-swap、archive、quarantine 竞态门禁继续覆盖 `os.open` 窗口；
- release required file 不稳定时 offline checker 返回 `release_file_unstable`。

目标 Ubuntu 账号使用 `umask=0002`。原 deployment 测试夹具用裸 `mkdir()` 创建 scratch，
因而得到 `0775`，并被 production checker 正确拒绝为“共享可写且无 sticky bit”；Windows
和常见 CI `0022` umask 没有暴露这一测试环境差异。夹具现在显式把自有 scratch 固定为
`0700`，production 权限门禁没有放宽，测试也不再依赖调用者 umask。

Windows 内容竞态压力门禁已运行 200 轮，每轮同时执行普通同长度改写和 mtime 恢复场景，
结果为 200/200，0 failure，0 error。最终代码与文档树的 Windows `npm run verify` PASS：
Python 448（421 pass、27 个平台能力 skip）、Schema 21/0、Data Audit healthy 0/0、
production build、Node 73/73、production dependency audit 0 vulnerabilities，以及 data、Git
与隔离 Runtime 四项保护门禁全部通过；Windows 如实跳过 Linux-only `systemd-analyze`。
目标 Ubuntu scratch release 的 deterministic test、200 轮压力、完整 Verify/build 与 GitHub
Ubuntu Verify 是 push 后的外部门禁，只在 Draft PR 和最终任务报告中记录，不在执行前预称通过。

## 数据、部署与回滚边界

本轮没有停止 Windows Primary、没有手工 refresh、没有清理 failed candidates，也没有
传输 data/D1、安装 systemd 或切换 `/opt/rardar/current`。服务器旧失败 release 保留为
证据；Ubuntu 验证只能在新 head 的独立 scratch release 中运行。

回滚只需回退本 hotfix 代码提交；它不引入 data、D1、Schema、manifest 或迁移格式变化。
下一次 `PROD-DEPLOY-01` 必须等本 PR 经人工 Ready、Squash merge 且 main Verify 成功后，
使用新的 exact main release；release Verify 必须先于 Windows Primary freeze 和新 cutover
backup。

## 是否影响 North Star

不改变 Weekly Acted Projects、评分、推荐、Stable Project ID 或产品 UI。它只修复支撑
audited generation、resolver 和部署检查的底层字节完整性证明。
