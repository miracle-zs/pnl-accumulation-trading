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
       | 3. 计算收益率 ret = (15mClose - avgPrice) / avgPrice        |
       +------------------------------+------------------------------+
                                      |
               +----------------------+----------------------+
               |                                             |
               v                                             v
+-------------------------------+             +-------------------------------+
|     触发止盈 / 分批平仓        |             |       空闲标的开仓检查        |
| - T1: ret >= 10% 全平并重开   |             | - 检查已审核且未持仓的标的    |
| - T2: 20% 平 50%, 50% 全平重开|             | - 按策略判断是否开 10U Tier 1 |
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

#### 2. 新币发现与人工审核
* 系统每日或启动时扫描 `exchangeInfo`。新发现的合约先标记为待审核，不参与开仓或补仓；
* 维护者确认其为原生加密资产后，将它加入已审核的标的分类文件。重启后系统加载该审核版本；符合正常交易条件的标的从下一轮开始进入策略判断，按策略决定是否开立 10 USDT Tier 1 底仓；
* **退市保护**：若交易所发布下架公告或合约状态变为 `SETTLING` / `CLOSE`，系统停止补仓，并在下架前最终交割结算。

#### 3. 资金规模与账户保证金测算（以 630 个原生币为例）
* **单币底仓参数**：名义价值 10 USDT，杠杆假设 10x；按名义价值除以杠杆估算，单币初始保证金为 1 USDT。
* **初始底仓估算**：630 个标的全部开立 T1 时，名义总额 6,300 USDT，初始保证金估算为 630 USDT。
* **历史路径估算**：当前简化回测的最大名义保证金估算为 1,534.04 USDT；在同一路径下，为使权益减去该保证金估算不为负，起始资金下限约为 2,480.73 USDT，且没有额外储备。
* 该数值不是实盘安全本金或强平结论。真实保证金取决于 Binance 合约风险限额、维持保证金、资金费、滑点和账户持仓。本系统不在下单前读取账户快照做保证金准入；买单由 Binance 接受或拒绝，拒单原因写入运行日志。上述估算不承诺“安全”或“绝无爆仓”。

---

### 3.2 仓位层级（Tier）状态流转与执行规则

系统严格执行 **3-Tier 网格 + 循环贬值复活机制**，全系统**零挂单**，所有止盈、补仓均由每 15 分钟收盘巡检触发市价单执行：

```
+---------------------------------------------------------------------------------------------------+
| Tier 1 (首仓底仓)                                                                                 |
| - 名义买入: 10 USDT (记录入场价 entry_t1)                                                          |
| - 15m 巡检浮盈 >= 10%: 市价全平，立刻在当前价格重新以 10 USDT 开仓 Tier 1 (完成一轮资金循环)     |
| - 15m 巡检跌幅触发: 收盘价 <= entry_t1 * 50% (较上一档买入价跌 >= 50%) -> 市价买入 20 USDT 进 T2   |
+---------------------------------------------------------------------------------------------------+
                                                  |
                                                  v (较 T1 买入价跌 >= 50%)
+---------------------------------------------------------------------------------------------------+
| Tier 2 (一级深度补仓，总持仓 30U，记录加仓价 entry_t2，刷新持仓均价)                               |
| - 止盈检查 (基于持仓均价 avg_price):                                                              |
|   * 浮盈 >= 20%: 平掉 50% 仓位 (初始 T2 仓位的一半)，进入 Tier 2.1 状态                           |
|   * 浮盈 >= 50%: 市价全平，清空仓位，立刻在当前价格重新以 10 USDT 开仓回到 Tier 1                 |
| - 下跌检查 (直接暴跌未反弹): 收盘价 <= entry_t2 * 50% (较 T2 买入价再跌 50%) -> 买入 60 USDT 进 T3|
+---------------------------------------------------------------------------------------------------+
                                                  |
                         +------------------------+------------------------+
                         | (若反弹 >= 20%)                                 | (若未反弹直接暴跌 >= 50%)
                         v                                                 |
+------------------------------------------------------------------+       |
| Tier 2.1 (半仓观察态，剩余持仓 15U)                              |       |
| - 止盈检查: 较均价浮盈 >= 50% -> 市价全平，重新开 10U 回到 Tier 1 |       |
| - 下跌检查: 收盘价 <= entry_t2 * 50% -> 买入 60 USDT 晋级为 Tier 3       |
+------------------------------------------------------------------+       |
                         | (再跌 50%)                                      |
                         +------------------------+------------------------+
                                                  |
                                                  v
+---------------------------------------------------------------------------------------------------+
| Tier 3 / 3.1 / 3.2 (二级极限补仓，记录加仓价 entry_t3，锁定铁底价 P_writeoff = 5.0 / Q_T3)         |
| - 买入名义: 60 USDT (总持仓名义价值增至 75U~90U，锁死单币最大投入上限 90U)                         |
| - 阶梯止盈 (基于持仓均价 avg_price):                                                              |
|   * 阶段一 (T3 -> T3.1): 浮盈 >= 20% -> 平仓 1/3，进入 T3.1                                       |
|   * 阶段二 (T3.1 -> T3.2): 浮盈 >= 50% -> 平仓剩余的一半 (即原仓位的 1/3)，进入 T3.2              |
|   * 阶段三 (T3.2 -> T1): 浮盈 >= 100% -> 平掉最后剩余全部仓位，清空重开 10U 回到 Tier 1          |
|                                                                                                   |
| 🧹 利润反哺注销重启制 (Profit-Funded Write-Off & Clean Restart):                                  |
| - 触发条件: 在 T3 / 3.1 / 3.2 阶段，每 15m 巡检若现价 c <= P_writeoff (残值跌破 5U，跌幅超 95%)   |
| - 核心哲学: 坚决不盲目加仓补无底洞，利用大盘已积累的近 1 万刀丰厚现金利润，直接打扫战场！       |
| - 执行动作:                                                                                       |
|   1. 市价全平割肉注销 (Market Close All)，将约 85U 亏损计入利润核销；                             |
|   2. 全平后立刻在当前价格以 10 USDT 重新开仓 Tier 1，开启新的交易周期。                           |
|   3. 账户 0 僵尸死仓，期末未结浮亏大幅收窄近 400 U，维持保证金完全释放！                         |
+---------------------------------------------------------------------------------------------------+
```

**止盈跨档规则**：每轮巡检按本轮收益率达到的最高止盈档处理，不逐轮补做已经跨过的低档。T2/T2.1 在浮盈达到 50% 时直接全平并重开 T1，即使此前尚未触发 20% 的 T2 第一批止盈。T3 在浮盈达到 100% 时全平并重开 T1；达到 50% 但未达到 100% 时，一次平掉 T3 初始仓位累计的 2/3 并进入 T3.2；达到 20% 但未达到 50% 时，平掉 T3 初始仓位的 1/3 并进入 T3.1。若已处于 T3.1，达到 50% 时再平掉 T3 初始仓位的 1/3；达到 100% 时全平并重开 T1。

---

### 3.3 全系统执行模式：100% 纯无状态 15 分钟市价流 (Pure Stateless Execution)

用户最终确认采用**全系统零挂单方案**：
* **彻底不挂任何限价单**：不在交易所保留任何未结挂单（Open Orders 为 0）。
* **所有买卖均在 15m 收盘瞬间判定**：
  * 止盈平仓：15m 收盘市价平仓；
  * 平仓后重开：15m 收盘市价开 10U；
  * DCA 跌破补仓：15m 收盘若价格 $\le$ 上一档价格的 50%，市价买入 20U / 60U；
  * 利润反哺注销：15m 收盘若现价 $\le P_{writeoff}$，市价全平注销死仓并重开 10U T1。
* **工程巨大优势**：无需处理 WebSocket 订单状态推送，无需处理由于价格剧烈跳动导致的限价单未成交/部分成交死锁。本地只需单向按时钟触发 REST API，单次耗时不超过 2 秒。

---

### 3.4 当前规则的历史回测统计

以下为此前基于本地回测脚本和行情缓存计算的历史结果（保存在 `data/`，该目录不纳入 Git，也不随代码仓库分发）。数据包含 630 个标的、26,208 根 15 分钟 K 线。模拟按 K 线收盘价成交，名义金额为 T1 10 USDT、T2 增仓 20 USDT、T3 增仓 60 USDT，Taker 手续费率为 0.05%；统计包含开仓、补仓、平仓和注销重开的手续费。

* **总净利润**：**+$5,430.12 USDT**（已实现净现金 + 期末未结仓位浮动盈亏）
* **已实现净现金**：**+$9,379.11 USDT**（扣除开仓、补仓及平仓手续费）
* **期末未结仓位浮动盈亏**：**-$3,948.99 USDT**
* **止盈事件**：**3,556 次**（部分止盈按每次成交事件计数）
* **补仓事件**：**862 次**
* **T3 注销并重开 T1**：**7 次**
* **累计手续费**：**56.79 USDT**

同一回测脚本还按 10x 名义初始保证金估算账户路径。起始资金 1,500 USDT 时最低权益为 -206.82 USDT、最大回撤 3,075.25 USDT（109.1%）；起始资金 2,000 USDT 时最低权益为 293.18 USDT、最大回撤 3,075.25 USDT（89.4%）。两种情景的最低可用权益分别为 -980.73 USDT 和 -480.73 USDT，表示历史路径中的部分时点无法覆盖估算的初始保证金。

账户路径是未加资金门禁的回放：即使可用权益为负，脚本仍继续执行后续交易。因此以上回撤与权益是压力诊断，不代表这些资金规模能真实执行完整策略。模型按 15 分钟收盘价成交、固定 10x 估算初始保证金；没有纳入 Binance 风险限额与维持保证金、强平、资金费、滑点、数量精度或动态标的准入。实盘安全本金仍未得到验证。

---

## 4. 15 分钟巡检调度执行细节 (Review Engine Execution)

### 4.1 触发时序
* 触发时间点设定在每 15 分钟收盘后的 **第 3~5 秒**（如 `12:00:04`, `12:15:04`, `12:30:04`, `12:45:04`）；
* 留出 3~5 秒延迟的目的是：等待币安完成上一根 15 分钟 K 线收盘，避免读取尚未完成的 K 线。

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
        
        # 按本轮 15 分钟 K 线收盘价计算策略收益率
        close_price = exchange.get_15m_close(symbol, slot_start_ms)
        ret = (close_price - pos.avg_price) / pos.avg_price
        
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
            # 优先判定：是否触发利润反哺注销重启 (close_price <= 5U / Q_T3)
            if state.writeoff_price > 0 and close_price <= state.writeoff_price:
                exchange.market_close_all(symbol) # 彻底平仓割肉注销
                db.record_trade(symbol, "WRITE_OFF_CLEANUP", pnl=pos.unrealized_pnl)
                # 立刻超跌底价重开全新 Tier 1 底仓
                exchange.market_open(symbol, notional=10.0)
                db.reset_tier(symbol, tier=1)
                notifier.send_alert(f"【打扫战场注销】{symbol} 触发归零注销，扣除利润核销，已在底价重启 T1")
                continue

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
    entry_price REAL NOT NULL,                 -- 当前持仓加权均价
    entry_t1 REAL NOT NULL,                    -- T1 初始开仓价
    entry_t2 REAL DEFAULT 0.0,                 -- T2 补仓触发价
    entry_t3 REAL DEFAULT 0.0,                 -- T3 补仓触发价
    writeoff_price REAL DEFAULT 0.0,           -- 铁底注销价格点 (5.0 / t3_initial_qty)
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

### 表 3：账户资产快照表 (`account_snapshots`，后续扩展)

该表用于未来的账户报表，不参与当前交易准入；当前精简交易闭环不请求或写入账户资产快照。
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

## 7. 告警与通知设计 (后续扩展)

本节能力不属于当前精简交易闭环，也不作为订单提交的前置条件。

1. **实时止盈推送（Webhook 卡片）**：
   * 内容包含：标的名称、触发层级、止盈收益率（如 `+18.4%`）、落袋纯现金、本轮开仓历时。
2. **补仓预警**：
   * 触发 Tier 2（跌 50%）或 Tier 3（再跌 50%）时发送提醒，同步最新均价。
3. **每日晨报汇总（后续扩展；不属于当前精简交易闭环）**：
   * 当日已实现净利润、累计收益曲线、当前总浮亏、持仓标的分布、当前账户保证金安全率。
4. **紧急熔断告警（后续扩展；当前不作为下单前置条件）**：
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
