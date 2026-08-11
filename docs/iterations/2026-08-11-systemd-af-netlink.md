# 2026-08-11 systemd AF_NETLINK hotfix

## 唯一目标

`PROD-DEPLOY-01` 在 exact Linux release 启动正式 systemd unit 时暴露了
`BLOCKED_SYSTEMD_AF_NETLINK`。本轮只把 Node/Vinext 枚举本机网络接口所需的
`AF_NETLINK` 纳入版本控制的 systemd sandbox 契约，并建立真实 Linux 正反对照证据。
本轮只创建 Draft PR，不恢复生产部署。

分支为 `fix/systemd-af-netlink`，基线是 PR #16 的 Squash merge 提交
`d20f3985277fc4a3656769774c1baecca82f414b`。P1-6C2 继续 deferred。

## Root cause

目标机是 Ubuntu 24.04、x86_64、systemd 255。Node/Vinext 启动期间会调用
`os.networkInterfaces()`；libuv 在 Linux 上为枚举接口使用 Netlink。原 versioned unit 只允许：

```text
AF_UNIX AF_INET AF_INET6
```

因此调用失败为：

```text
uv_interface_addresses
errno 97
exit 1
```

同一目标机、同一 Node probe 的 transient systemd A/B 已证明：省略 `AF_NETLINK`
稳定失败，增加 `AF_NETLINK` 后成功。问题不是 Node 或 Vinext bug，也不是公网或信源故障；
它是 versioned sandbox contract 与真实 Runtime syscall 需求不一致。

## 最小修复

唯一 production 配置变化是：

```ini
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
```

没有增加 `AF_PACKET`、raw packet capability 或 network administration capability。没有修改
`CapabilityBoundingSet`、`AmbientCapabilities`、`NoNewPrivileges`、`ProtectSystem`、
`ProtectHome`、`PrivateDevices`、kernel hardening、`ReadWritePaths`、`KillMode`、service user
或 loopback binding。

回滚只需恢复该单行 unit 变化；本轮没有 data、D1、Schema、generation 或迁移变化。

## Versioned regression contract

Verify 解析 `[Service]` directive，而不是只搜索 `AF_NETLINK` 字符串。门禁要求：

- `RestrictAddressFamilies` 恰好有一个 authoritative 定义；
- effective token set 精确为 `AF_UNIX`、`AF_INET`、`AF_INET6`、`AF_NETLINK`；
- token 不重复，且不包含 `AF_PACKET` 或 `AF_RAW`；
- non-root、empty capability sets、filesystem/kernel hardening、control-group cleanup 保持原值；
- Website 与 Runtime status 的 production host 继续固定为 `127.0.0.1`。

版本化 probe 只调用 Node `os.networkInterfaces()`，只验证返回值是合法 object，并输出
`AF_NETLINK_PROBE_OK`。它不访问公网、不发送网络包、不监听端口、不读写 Rardar data，
也不依赖接口名、IP 地址或公网接口存在。

## Linux runtime evidence gate

Draft PR 前必须在目标 Ubuntu 的新 `<HOTFIX_HEAD>` scratch release 完成：

1. exact checkout、locked Python/Node dependencies、build 与完整 `npm run verify`；
2. `systemd-analyze verify deploy/systemd/rardar.service`；
3. transient positive unit 使用四个 address families，probe 输出
   `AF_NETLINK_PROBE_OK` 且退出 0；
4. transient negative control 使用原三个 families，同一 probe 重现
   `uv_interface_addresses` / errno 97 等价失败；
5. transient units 完整清理，正式 `rardar.service` 继续 disabled/inactive，3000/3002 空闲；
6. `/opt/rardar/current` 不变，canonical data 与 Vinext/D1 tree SHA 前后完全相同。

目标机完整输出、Windows Verify 和 GitHub Verify 结论记录在 Draft PR 与最终任务报告中，
不在执行前预称通过。

## 数据、部署与安全边界

Windows Primary 在整个 hotfix 阶段保持 active，不停止、不 refresh、不修改 `nextRunAt`。
服务器既有 canonical data/Vinext 保持只读，不重传、不清理 failed candidates，也不安装或启动
本 hotfix 的正式 service。`/opt/rardar/current` 与既有 exact main release保持不变。

本轮不修改 DNS、TLS、Nginx、防火墙、公开监听或 secret，不开始 P1-6C2、TrendRadar/P2。
AF_NETLINK 只授权内核网络接口发现通信，不授权 raw packet、公网监听或网络管理。

## 合并后的恢复策略（仅记录）

只有本 Draft PR 经人工 Ready、Squash merge 且 main Verify 成功后，下一次明确授权的
`PROD-DEPLOY-01` 才能构建 merge 后 exact release。恢复部署前先比较 Windows Primary 与
服务器 canonical data/Vinext/D1；完全一致时不重传，不一致时重新建立正式 cutover source。

## 是否影响 North Star

不改变 Weekly Acted Projects、评分、推荐、Stable Project ID、generation 发布或 UI。
它只修复 Always-on Runtime 的最小 Linux sandbox compatibility 契约。
