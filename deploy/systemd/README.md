# Linux systemd 部署

`pnl-trader.service` 连接 Binance USDⓈ-M Futures 主网。启动后会运行巡检，并可能提交真实市价单；当前工作区没有安装或启动该服务。

项目和虚拟环境默认安装在 `/opt/pnl-accumulation-trading`。部署时需在 `/etc/pnl-trader/pnl-trader.env` 设置：

```ini
PNL_ACCOUNT_ID=<账户标识>
PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml
PNL_MAX_MAINTENANCE_MARGIN_RATIO=<经审核的上限>
PNL_RESERVE_BUFFER_USDT=<预留 USDT>
PNL_SLIPPAGE_BUFFER_RATE=<滑点缓冲>
```

API 凭据分别保存在 root 所有、权限为 `0600` 的文件中：

- `/etc/pnl-trader/credentials/binance-api-key`
- `/etc/pnl-trader/credentials/binance-api-secret`

启用前先检查 unit 文件和本地配置：

```sh
systemd-analyze verify /etc/systemd/system/pnl-trader.service
pnl-trader validate-config
```

不要在未确认账户、标的分类和风险参数时启动服务。
