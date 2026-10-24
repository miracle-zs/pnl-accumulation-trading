# 15 分钟收益统计自动交易系统

本仓库的产品目标以 [PRD](docs/PRD_15M_PERIODIC_REVIEW_TRADING.md) 为准：构建 15 分钟定时巡检的自动交易系统。当前实现先聚焦巡检、策略决策、订单确认和 SQLite 状态保存。

## 当前文档入口

- [PRD](docs/PRD_15M_PERIODIC_REVIEW_TRADING.md)：当前唯一业务需求依据。
- [架构设计](docs/ARCHITECTURE_15M_PERIODIC_REVIEW_TRADING.md)：从 PRD 推导的模块边界、状态模型、执行和恢复设计。
- [术语表](GLOSSARY.md)：项目领域术语。

需求与实现不一致时，先按 PRD 判断产品行为，再更新架构与实现；不要从历史回测脚本或旧设计推断当前交易规则。

## 开发与运行

安装项目：

```sh
python -m pip install -e .
```

运行本地自动化测试：

```sh
python -m unittest discover -s tests -v
```

交易运行入口连接 Binance USDⓈ-M Futures 主网，启动后可能提交真实市价单。不要在未确认账户、标的分类和风险参数时启动服务，也不要将密钥提交到仓库。

## 历史资料

- [旧 SPEC](docs/SPEC.md)描述此前的事件驱动、限价挂单和市值缩水循环方案，不是当前系统规范。
- `docs/adr/` 保存早期架构决策。引用其中任何决策前，先确认它仍符合当前 PRD 和架构设计。
- 根目录的历史回测、模拟和数据处理脚本，以及 `data/` 下的缓存，属于研究材料；它们不等于当前运行服务，也不构成实盘安全或收益保证。
- `docs/ARCHITECTURE_15M_OVERVIEW.html` 是架构图解材料；若与 Markdown 架构文档不一致，以 Markdown 为准。
