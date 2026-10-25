# Linux systemd 部署

`pnl-trader.service` 连接 Binance USDⓈ-M Futures 主网。启动后会运行巡检，并可能提交真实市价单；当前工作区没有安装或启动该服务。

不带子命令运行 `pnl-trader` 只显示帮助。只有显式执行 `pnl-trader run` 才会启动主网交易服务；systemd unit 也使用该命令。

项目和虚拟环境默认安装在 `/opt/pnl-accumulation-trading`。部署时需在 `/etc/pnl-trader/pnl-trader.env` 设置：

```ini
PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml
```

API 凭据分别保存在 root 所有、权限为 `0600` 的文件中：

- `/etc/pnl-trader/credentials/binance-api-key`
- `/etc/pnl-trader/credentials/binance-api-secret`

启用前先检查 unit 文件和本地配置：

```sh
systemd-analyze verify /etc/systemd/system/pnl-trader.service
PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml \
PNL_DATA_DIR=/var/lib/pnl-trader \
/opt/pnl-accumulation-trading/.venv/bin/pnl-trader validate-config
```

`validate-config` 只检查本地配置和资产分类，不需要 Binance API 凭据。

不要在未确认账户和标的分类时启动服务。
