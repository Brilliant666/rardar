# 2026-07-11 · Data contracts

## 目标

为七类核心 JSON 产物建立版本化 Schema 和统一 Python 验证入口，使无效 Codex enrichment 与派生产物在正式替换前失败。

## 完成内容

- 新增七份 Draft 2020-12 JSON Schema；
- 新增严格 JSON 解析、格式检查、身份检查和完整数据树验证；
- 将验证接入采集、静态分析、catalog、queue、refresh、derive 与 audit；
- 新增受共享锁保护的 Codex enrichment 草稿 ingest，验证后才原子替换正式文件；
- 为既有静态证据和项目画像增加显式版本，不伪造缺失时间；
- 增加合法、类型错误、数组成员、URL、时间、repository、版本、长度、嵌套字段、审计报告和写入前拒绝测试。

## 验证结果

- `npm run lint`：通过；
- Python：97 项测试通过；
- `npm run data:validate`：20 份正式产物通过，0 错误；
- `npm run data:audit`：`healthy`，0 错误、0 警告；
- Node.js 22.13.1 下 `npm run build`：通过；
- Node.js 22.13.1 下 `npm test`：2 项渲染测试通过；
- `npm run security:audit:prod`：0 个生产依赖漏洞。

## 新发现

- Snapshot v1 的 history 与 latest 具有兼容的基础/扩展形状；
- 两份旧静态证据没有可信 `analyzed_at`，已显式保留为 legacy v0；
- Signal enrichment v1 同时存在顶层时间回退和逐条时间绑定。

## 遗留风险

- Schema 不负责跨文件语义一致性；
- 手工绕过 Python 入口仍可直接编辑 JSON，后续应由统一 verify 与 CI 阻止合入；
- generation 原子发布、稳定项目 ID 和追加式行动事件尚未实现。

## 是否影响 North Star

不改变北极星定义或指标值；它提高支撑指标与推荐数据的可信度。

## 建议的下一项最高优先级

本 PR 合并后，按协议下一项应是“审计通过后才发布 generation”。本轮不会自动开始。
