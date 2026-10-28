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

看板只读取 `PNL_DATA_DIR` 下的 `state.sqlite3`，不会连接交易所或提交订单。交易服务每轮策略处理结束后读取一次 Binance 账户数据，展示权益曲线、未实现盈亏、可用余额、持仓总价值和整体实际杠杆（持仓总价值除以账户权益）；数据不参与策略决策。新增账户指标从升级后首次采样开始显示。

数据库使用 schema v24；支持从 v21、v22、v23 按顺序升级。看板样式由 `frontend/` 中锁定版本的 Tailwind CLI 编译为本地静态文件，构建命令为 `cd frontend && npm ci && npm run build`。

运行本地自动化测试：

```sh
python -m unittest discover -s tests -v
```

交易运行入口连接 Binance USDⓈ-M Futures 主网，启动后可能提交真实市价单。标的池由 Binance `exchangeInfo` 自动筛选活跃的 USDT 永续 `COIN`、`INDEX` 合约及 `underlyingSubType` 含 `RWA` 的合约；符合条件的新合约会自动纳入。不要在未确认账户和风险参数时启动服务，也不要将密钥提交到仓库。

可选配置 `SERVERCHAN_SENDKEY` 接收整轮巡检失败提醒；不配置时只写服务日志，不影响交易服务启动。

## 历史资料

- [旧版 SPEC](docs/SPEC.md)：记录此前的事件驱动、限价挂单和市值缩水循环方案，仅供追溯。
- [ADR 历史记录说明](docs/adr/README.md)：ADR 保留当时的决策背景；部分内容已与当前方案不同，当前行为以 PRD 和架构设计为准。
- `data/` 仅存放本地回测与研究材料，已从 Git 版本控制中排除。交易服务从 Binance 获取合约目录，运行状态目录必须通过 `PNL_DATA_DIR` 显式指定。
- `research/` 下的历史回测、模拟和数据处理脚本不属于当前交易服务，也不构成实盘安全或收益保证。
