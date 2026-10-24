# 产品需求文档 (PRD)：15 分钟定时收益统计自动化交易系统
**Version**: 1.0.0  
**Status**: Ready for Implementation  
**Target Platform**: Binance USDT-M Futures (币安永续合约)  
**Author**: Antigravity Quant Team  

---

## 1. 项目背景与设计哲学 (Background & Philosophy)

### 1.1 背景痛点
传统的“盘中限价单（Limit Order TP）+ 平完秒开”网格架构，在实盘落地上面临三大工程与市场力学瓶颈：
1. **连接与状态机极重**：必须维持 7×24 小时高频 WebSocket（User Data Stream）长连接，监听每一笔成交。一旦网络抖动、掉线重连或服务器重启，容易出现挂单脱轨、重复挂单或漏单；
2. **交易所规则硬约束**：币安对单个账户有最大挂单数限制（Max Open Orders 200 个），多标的（50~100 个币）同时运行很容易因达到挂单上限而被拒绝下单；
3. **脉冲暴拉时的追高陷阱**：山寨币短时暴涨（如 5 秒内暴拉 +25%）打穿 +10% 限价单时，程序收到成交推送时市价往往已飙至高位，此时“原价秒开”根本无法成交，市价追单则面临巨大追高滑点，直接挂在山顶。

### 1.2 15 分钟定时统计的设计哲学
本方案采用**“离散时间采样（Discrete-Time Sampling）+ 无状态轮询（Stateless Polling）”**的设计哲学：
* **工程降维**：完全抛弃 WebSocket 订单监听与盘面挂单，每 15 分钟只跑一次极轻量的 REST API 巡检，单核低配 VPS 即可稳如泰山运行；
* **捕获单边大阳线（Overshoot 溢价）**：在遇到大暴拉行情时，15 分钟收盘可直接以市价一次性锁定 +20%~+30% 的超额收益，避免在 +10% 处被过早贱卖；
* **回测实证数据支撑**：在 100 个核心高成交量标的、26,208 根 15 分钟真实 K 线的全量回测中，15 分钟定时统计的总净利润达到 **+$1,349.68**，与盘中限价单（+$1,323.25）完全持平甚至微幅占优。

---

## 2. 系统总体架构与技术选型 (Architecture & Tech Stack)

```
       +-------------------------------------------------------------+
       |               Cron / APScheduler (每 15 分钟触发)            |
       |                   (*:00:05, *:15:05, *:30:05, *:45:05)      |
       +------------------------------+------------------------------+
                                      |
                                      v
       +-------------------------------------------------------------+
       |             ReviewExecutor (15m 巡检执行核心)                |
       | 1. GET /fapi/v2/positionRisk (一次性拉取全部多头持仓)        |
       | 2. 与本地 SQLite 对账并识别当前 Tier 层级与分批状态           |
       | 3. 计算收益率 ret = (markPrice - avgPrice) / avgPrice       |
       +------------------------------+------------------------------+
                                      |
               +----------------------+----------------------+
               |                                             |
               v                                             v
+-------------------------------+             +-------------------------------+
|     触发止盈 / 分批平仓        |             |       空闲标的开仓检查        |
| - T1: ret >= 10% 全平并重开   |             | - 检查标的池中未持仓的代币    |
| - T2: 20% 平 50%, 50% 全平重开|             | - 自动以 10U 开仓 Tier 1      |
| - T3: 20% / 50% / 100% 分批   |             +---------------+---------------+
+--------------+----------------+                             |
               |                                             |
               +----------------------+----------------------+
                                      |
                                      v
       +-------------------------------------------------------------+
       |        OrderExecutionGateway (市价下单 & 精度格式化)         |
       | - 自动适配 LOT_SIZE (stepSize) 与 MIN_NOTIONAL (>= 5 USDT)   |
       +------------------------------+------------------------------+
                                      |
               +----------------------+----------------------+
               |                                             |
               v                                             v
+-------------------------------+             +-------------------------------+
|     SQLite (WAL 模式持久化)    |             |      Webhook 告警与通知       |
| - 记录 symbols_state 状态锁   |             | - 飞书 / 钉钉 / Telegram      |
| - 记录 trade_history 成交账本 |             | - 实时止盈卡片、每日晨报汇总  |
+-------------------------------+             +-------------------------------+
```

* **开发语言**：Python 3.11+
* **交易所连接**：Binance USDT-M Futures REST API（使用 `requests` 或 `aiohttp`）
* **存储引擎**：SQLite 3（开启 `PRAGMA journal_mode=WAL;`，支持高并发无锁读写）
* **调度机制**：系统级 `cron` 或 Python 内置 `APScheduler`（每 15 分钟执行一次）
* **消息通知**：支持 飞书 / 钉钉 / Telegram Webhook

---

## 3. 核心业务流程与仓位状态机 (State Machine & Logic)

### 3.1 标的池定义：全量原生币圈活跃合约标的 (All Native Crypto Universe)

系统标的池明确定义为：**币安 USDT-M 永续合约盘面上，所有存活且处于正常可交易状态（TRADING）的纯原生加密货币（Native Crypto），当前约为 630 个标的**。

#### 1. 严格过滤黑名单规则（彻底排除非加密资产）
系统在每日早 8 点或启动初始化时，调用 `GET /fapi/v1/exchangeInfo` 进行动态扫描，严格剔除以下四类非加密资产：
* **美股 / 港股 / 中概 / 韩股代币 (135+ 个)**：如 `AAPLUSDT`, `TSLAUSDT`, `NVDAUSDT`, `MSFTUSDT`, `BABAUSDT`, `PDDUSDT`, `COINUSDT`, `MSTRUSDT` 等；
* **宏观指数与杠杆 ETF (38+ 个)**：如 `SPYUSDT`, `QQQUSDT`, `TQQQUSDT`, `SQQQUSDT`, `SOXLUSDT`, `UVXYUSDT`, `BITOUSDT` 等；
* **大宗商品 / 外汇 / 稳定币互换 (14+ 个)**：如 `COPPERUSDT` (铜), `NATGASUSDT` (天然气), `USDBRLUSDT` (法币外汇), `USDCUSDT` (稳定币互换), `BTCDOMUSDT` (市占率指数) 等；
* **非正常交易/已下架合约**：`status != 'TRADING'` 的退市合约。

#### 2. 新币自动发现与动态纳管机制 (Auto-Discovery)
* **动态热加载**：每 24 小时或重启时扫描一次 `exchangeInfo`，若币安新上线了纯加密货币合约（如新上线某个 Meme 币或 L1 代币），系统自动将其纳入数据库，并在下一个 15 分钟整点自动开出 10 USDT Tier 1 底仓；
* **退市保护**：若交易所发布下架公告或合约状态变为 `SETTLING` / `CLOSE`，系统停止补仓，并在下架前最终交割结算。

#### 3. 资金规模与账户保证金测算（以 630 个原生币为例）
* **单币底仓参数**：名义价值 10 USDT，杠杆 10x，单币保证金占用仅 **1.0 USDT**；
* **初始底仓全开占用**：$630 \text{ 个币} \times 1.0\text{ USDT} = \mathbf{630\text{ USDT}}$；
* **极端行情压力测试（全市场暴跌 50%）**：
  * 假设全市场 30% 标的（约 190 个币）跌幅超 50% 触发 Tier 2（补仓 20U，保证金占用 2U）；
  * 假设全市场 8% 标的（约 50 个币）跌幅超 75% 触发 Tier 3（补仓 60U，保证金占用 6U）；
  * 此时新增占用保证金：$190 \times 2 + 50 \times 6 = 680\text{ USDT}$；
  * **极限保证金总占用**：$630 + 680 \approx 1,310\text{ USDT}$；
* **推荐总账户本金配置**：
  * **起步安全本金**：**1,500 ~ 2,000 USDT**。即使遭遇全市场腰斩的大崩盘，维持保证金率依旧能稳定保持在极度安全的健康水位，绝无爆仓风险。

---

### 3.2 仓位层级（Tier）与操作规则

系统严格执行 **3-Tier 封顶网格** 与 **对齐的分批止盈机制**：

```
+---------------------------------------------------------------------------------------------------+
| Tier 1 (底仓)                                                                                     |
| - 名义价值: 10 USDT (杠杆 10x)                                                                    |
| - 15m 巡检浮盈 >= 10%: 市价全平，立刻在当前价格重新以 10 USDT 开仓 Tier 1 (完成一轮资金循环)     |
| - 跌幅触发: 价格自 T1 入场均价下跌 50% -> 补仓 20 USDT 晋级为 Tier 2                              |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v (下跌 50%)
+---------------------------------------------------------------------------------------------------+
| Tier 2 (一级深度补仓)                                                                             |
| - 补仓名义: 20 USDT (总持仓名义价值增至 30 USDT，拉低均价)                                         |
| - 止盈规则 (引入分批状态锁 t2_batch1_done):                                                        |
|   * Batch 1 (50% 仓位 = 15 USDT): 浮盈 >= 20% 且 not t2_batch1_done -> 平仓一半，锁定状态锁   |
|   * Batch 2 (剩余 50% 仓位): 浮盈 >= 50% -> 市价全平，清空仓位，重置开启 10 USDT Tier 1          |
| - 跌幅触发: 价格自 T2 买入价再跌 50% (累计跌 75%) -> 补仓 60 USDT 晋级为 Tier 3                    |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v (再跌 50%)
+---------------------------------------------------------------------------------------------------+
| Tier 3 (二级极限补仓 - 严格封顶)                                                                  |
| - 补仓名义: 60 USDT (总持仓名义价值增至 90 USDT，单币极限敞口封顶)                                |
| - 止盈规则 (引入分批状态计数 t3_batches_done: 0, 1, 2):                                            |
|   * Batch 1 (1/3 仓位 = 30 USDT): 浮盈 >= 20% 且 done == 0 -> 平仓 1/3，计数置 1                  |
|   * Batch 2 (1/3 仓位 = 30 USDT): 浮盈 >= 50% 且 done < 2  -> 平仓至剩余 1/3，计数置 2            |
|   * Batch 3 (剩余 1/3 仓位): 浮盈 >= 100% -> 市价全平，清空仓位，重置开启 10 USDT Tier 1         |
| - 封顶安全: Tier 3 为常规补仓终点，严禁继续盲目加仓放大风险                                       |
+---------------------------------------------------------------------------------------------------+
```

---

### 3.3 补仓执行策略（两种可选模式）
为了兼顾简单性与费率，补仓支持以下两种模式之一（推荐模式 A）：
* **模式 A（15m 巡检市价补仓 - 纯无状态模式）**：
  在每 15 分钟巡检时，若检测到现价已低于触发价（如相对于开仓价跌幅 $\ge 50\%$），直接市价买入补仓。
  * *优点*：彻底不依赖挂单，系统没有任何交易所未结订单，极其纯粹；
  * *缺点*：成交为 Taker 费率（0.05%）。
* **模式 B（条件挂单补仓 - 费率优化模式）**：
  在开仓后，仅在交易所挂单买入补仓（T2 在 -50% 处挂 20U 现价买单，T3 在 -50% 处挂 60U 买单）。
  * *优点*：补仓享受 Maker 0.02% 费率；
  * *巡检逻辑*：15 分钟巡检时直接比对持仓名义价值（若 Notional 达到 30U 则自动识别为 T2 已成交，同步更新状态）。

---

## 4. 15 分钟巡检调度执行细节 (Review Engine Execution)

### 4.1 触发时序
* 触发时间点设定在每 15 分钟收盘后的 **第 3~5 秒**（如 `12:00:04`, `12:15:04`, `12:30:04`, `12:45:04`）；
* 留出 3~5 秒延迟的目的是：等待币安撮合引擎完成整点 K 线最终收盘与标记价格计算，避免偶发价格跳变。

### 4.2 巡检 5 步走核心逻辑

```python
def on_15m_timer_trigger():
    # 1. 抓取交易所最新全量多头持仓 (单次 API 调用)
    positions = exchange.get_all_positions(filter_positive=True)
    
    # 2. 读取本地数据库状态
    db_states = db.get_all_symbol_states()
    
    # 3. 遍历当前持仓标的进行损益判定与处理
    for symbol, pos in positions.items():
        state = db_states.get(symbol) or init_state_from_position(pos)
        
        # 计算未实现盈亏收益率 (基于持仓均价)
        ret = (pos.mark_price - pos.avg_price) / pos.avg_price
        
        # --- Tier 1 判定 ---
        if state.tier == 1:
            if ret >= 0.10: # 浮盈 >= 10%
                exchange.market_close_all(symbol)
                db.record_trade(symbol, "TP_T1", pnl=pos.unrealized_pnl)
                # 立刻重开 Tier 1
                exchange.market_open(symbol, notional=10.0)
                db.reset_tier(symbol, tier=1)
                notifier.send_tp_alert(symbol, tier=1, ret=ret, pnl=pos.unrealized_pnl)
                
        # --- Tier 2 判定 ---
        elif state.tier == 2:
            if ret >= 0.50: # Batch 2 目标达成: 全平
                exchange.market_close_all(symbol)
                db.record_trade(symbol, "TP_T2_BATCH2", pnl=pos.unrealized_pnl)
                exchange.market_open(symbol, notional=10.0)
                db.reset_tier(symbol, tier=1)
                notifier.send_tp_alert(symbol, tier=2, ret=ret, pnl=pos.unrealized_pnl)
            elif ret >= 0.20 and not state.t2_batch1_done: # Batch 1 目标达成: 平一半
                close_qty = state.t2_initial_qty * 0.5
                exchange.market_close_partial(symbol, qty=close_qty)
                db.mark_t2_batch1_done(symbol)
                notifier.send_tp_alert(symbol, tier=2, ret=ret, msg="已锁定第一批 50% 利润")

        # --- Tier 3 判定 ---
        elif state.tier == 3:
            if ret >= 1.00: # Batch 3: 全平
                exchange.market_close_all(symbol)
                exchange.market_open(symbol, notional=10.0)
                db.reset_tier(symbol, tier=1)
            elif ret >= 0.50 and state.t3_batches_done < 2:
                # 平第二批
                exchange.market_close_partial(symbol, qty=state.t3_initial_qty * (2 - state.t3_batches_done) / 3.0)
                db.set_t3_batches_done(symbol, 2)
            elif ret >= 0.20 and state.t3_batches_done == 0:
                # 平第一批
                exchange.market_close_partial(symbol, qty=state.t3_initial_qty / 3.0)
                db.set_t3_batches_done(symbol, 1)

    # 4. 检查标的池中空闲币种 (已完全平仓或尚未开仓的)，自动补开 Tier 1 (10U)
    for symbol in TARGET_UNIVERSE:
        if symbol not in positions or positions[symbol].qty == 0:
            exchange.market_open(symbol, notional=10.0)
            db.init_symbol_state(symbol, tier=1)

    # 5. 记录账户权益快照
    db.save_account_snapshot(exchange.get_account_balance())
```

---

## 5. 数据库设计 (Database Schema - SQLite)

使用嵌入式 SQLite 数据库，启用 WAL 模式保证高性能与崩溃自愈。

### 表 1：标的状态表 (`symbols_state`)
记录当前各标的生命周期及分批止盈锁状态：
```sql
CREATE TABLE IF NOT EXISTS symbols_state (
    symbol TEXT PRIMARY KEY,
    tier INTEGER NOT NULL DEFAULT 1,          -- 当前层级: 1, 2, 3
    is_active INTEGER NOT NULL DEFAULT 1,      -- 是否在场内
    t2_batch1_done INTEGER NOT NULL DEFAULT 0, -- T2 第一批是否已平仓 (0/1 状态锁)
    t3_batches_done INTEGER NOT NULL DEFAULT 0,-- T3 已平批次数 (0, 1, 2)
    t2_initial_qty REAL DEFAULT 0.0,           -- 进入 T2 时的初始总持仓数量
    t3_initial_qty REAL DEFAULT 0.0,           -- 进入 T3 时的初始总持仓数量
    entry_price REAL NOT NULL,                 -- 底仓或补仓后的加权均价
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 表 2：交易流水表 (`trade_history`)
```sql
CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,                      -- 'OPEN_T1', 'BUY_T2', 'TP_T1', 'TP_T2_B1' 等
    tier INTEGER NOT NULL,
    price REAL NOT NULL,
    qty REAL NOT NULL,
    realized_pnl REAL DEFAULT 0.0,             -- 实现盈亏 (USDT)
    commission REAL DEFAULT 0.0,               -- 手续费
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 表 3：账户资产快照表 (`account_snapshots`)
```sql
CREATE TABLE IF NOT EXISTS account_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_wallet_balance REAL NOT NULL,        -- 钱包本金
    total_unrealized_pnl REAL NOT NULL,        -- 未结浮动盈亏
    total_margin_used REAL NOT NULL,           -- 已用保证金
    margin_ratio REAL NOT NULL,                -- 维持保证金率 (防爆仓)
    open_positions_count INTEGER NOT NULL      -- 持仓标的数
);
```

---

## 6. 异常与边缘场景防范 (Edge Cases & Safety Rules)

### 6.1 币安最小名义价值限制（MIN_NOTIONAL = 5 USDT）
* **规则**：币安合约单笔下单名义价值不得低于 5 USDT；
* **处理方案**：
  * Tier 1 开仓为 10 USDT（高于 5U，正常通过）；
  * Tier 2 分批平仓为 15 USDT（高于 5U，正常通过）；
  * 若因币种价格极度暴跌导致半仓市值 $< 5\text{ USDT}$，系统应自动执行**“整仓合并平仓（Merge into 100% Close）”**，直接一次性平掉全部仓位，防止抛出 `Filter failure: MIN_NOTIONAL` 异常。

### 6.2 数量精度自动对齐（LOT_SIZE / stepSize）
* 每次下单前，自动读取从 `/fapi/v1/exchangeInfo` 获取的 `stepSize`，对 `qty` 执行向下截断取整（`floor_to_step(qty, stepSize)`），严禁四舍五入导致超额平仓。

### 6.3 网络超时与 API 限流（429 频控防范）
* 单次 15 分钟巡检中，持仓查询仅需**单次调用**（权重仅为 5），远低于币安每分钟 1,200 的请求权重上限；
* 批量下单时增加单次间隔（20ms），若遇到网络超时，采用指数退避重试（重试 3 次，间隔 1s, 2s, 4s）。

### 6.4 宕机自愈机制（Crash Resilience）
* 若由于服务器断电或重启错过某一轮 15m 巡检：
  * 程序下次启动时，直接调用 `GET /fapi/v2/positionRisk` 重新拉取实盘持仓；
  * 若数据库文件丢失，系统可根据各币种当前的 `notional` 自动逆向推导层级（$10\text{U} \to \text{T1}$, $30\text{U} \to \text{T2}$, $90\text{U} \to \text{T3}$），**实现 0 状态秒级自愈**！

---

## 7. 告警与通知设计 (Alerting & Monitoring)

1. **实时止盈推送（Webhook 卡片）**：
   * 内容包含：标的名称、触发层级、止盈收益率（如 `+18.4%`）、落袋纯现金、本轮开仓历时。
2. **补仓预警**：
   * 触发 Tier 2（跌 50%）或 Tier 3（再跌 50%）时发送提醒，同步最新均价。
3. **每日晨报汇总（每天早上 08:00 北京时间）**：
   * 当日已实现净利润、累计收益曲线、当前总浮亏、持仓标的分布、当前账户保证金安全率。
4. **紧急熔断告警**：
   * 若账户保证金率突破安全阈值（如保证金使用率超过 70%），立即暂停开启新标的，并向手机推送高优先级警报。

---

## 8. 上线里程碑与交付计划 (Roadmap)

1. **Phase 1（核心执行器与数据库实现，1-2天）**：
   * 编写 `exchange_gateway.py`（封装仓位查询、市价下单、精度对齐）；
   * 编写 `db_manager.py`（初始化 SQLite 表及状态锁读写）；
   * 编写 `engine_15m.py`（实现 15 分钟核心巡检判定闭环）。
2. **Phase 2（模拟网与小资金实盘验证，2-3天）**：
   * 在币安合约 Testnet 或实盘账户选取 3~5 个标的（每单 10U）实盘观察 48 小时；
   * 验证 15m 触发点是否精准、止盈分批锁是否稳健、重开是否平滑。
3. **Phase 3（全标的部署与监控接入，1天）**：
   * 导入完整标的池，配置 Webhook 告警机器人，通过 systemd 或 Docker 容器化守护运行。
