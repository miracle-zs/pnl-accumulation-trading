# 15 分钟收益统计自动交易系统

本仓库的产品目标以 [PRD](docs/PRD_15M_PERIODIC_REVIEW_TRADING.md) 为准：构建 15 分钟定时巡检的自动交易系统。当前实现先聚焦巡检、策略决策、订单确认和 SQLite 状态保存。

## 当前文档

- [PRD](docs/PRD_15M_PERIODIC_REVIEW_TRADING.md)：当前唯一业务需求依据。
- [架构设计](docs/ARCHITECTURE_15M_PERIODIC_REVIEW_TRADING.md)：从 PRD 推导的模块边界、状态模型、执行和恢复设计。
- [术语表](GLOSSARY.md)：项目领域术语。

当前交易规则以 PRD 为准，架构设计说明实现边界。旧版规格、ADR、历史回测和图解均不是当前规范；不要据此推断交易规则。

## 开发与运行

安装项目：

```sh
python -m pip install -e .
```

启动本地只读看板（默认仅绑定 `127.0.0.1`，需显式指定数据库目录）：

```sh
PNL_DATA_DIR=./var pnl-trader web
```

也可以传入 `--data-dir` 覆盖环境变量。

看板只读取 `PNL_DATA_DIR` 下的 `state.sqlite3`，不会连接交易所或提交订单。它展示成交账本和本地策略状态，不展示账户权益或保证金快照。

当前版本只接受空目录创建的新数据库，不迁移旧数据库。已有 `state.sqlite3` 会保留原样；需要重新开始时，将 `PNL_DATA_DIR` 指向一个新的空目录。

运行本地自动化测试：

```sh
python -m unittest discover -s tests -v
```

交易运行入口连接 Binance USDⓈ-M Futures 主网，启动后可能提交真实市价单。不要在未确认账户、标的分类和风险参数时启动服务，也不要将密钥提交到仓库。

可选配置 `SERVERCHAN_SENDKEY` 接收整轮巡检失败提醒；不配置时只写服务日志，不影响交易服务启动。

## 历史资料

- [旧版 SPEC](docs/SPEC.md)：记录此前的事件驱动、限价挂单和市值缩水循环方案，仅供追溯。
- [ADR 历史记录说明](docs/adr/README.md)：ADR 保留当时的决策背景；部分内容已与当前方案不同，当前行为以 PRD 和架构设计为准。
- `data/` 仅存放本地回测与研究材料，已从 Git 版本控制中排除。当前交易服务使用 `config/` 中的生产配置，运行状态目录必须通过 `PNL_DATA_DIR` 显式指定。
- `research/` 下的历史回测、模拟和数据处理脚本不属于当前交易服务，也不构成实盘安全或收益保证。
