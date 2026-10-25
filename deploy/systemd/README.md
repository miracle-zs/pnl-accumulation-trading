# Linux systemd 部署

`pnl-trader.service` 连接 Binance USDⓈ-M Futures 主网。启动后会运行巡检，并可能提交真实市价单；当前工作区没有安装或启动该服务。

不带子命令运行 `pnl-trader` 只显示帮助。只有显式执行 `pnl-trader run` 才会启动主网交易服务；systemd unit 也使用该命令。

## 安装与预检（不启动）

目标主机需运行 Linux、systemd 247 或更新版本（本 unit 使用 `LoadCredential=`），并安装 Git 和包含 `venv`、`pip` 的 Python 3.11 或更新版本。以下步骤只安装和验证，不启用或启动服务。

1. 创建服务用户（已存在时跳过），克隆项目并安装：

   ```sh
   sudo useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin pnl-trader
   sudo git clone https://github.com/miracle-zs/pnl-accumulation-trading.git /opt/pnl-accumulation-trading
   cd /opt/pnl-accumulation-trading
   sudo python3 -m venv .venv
   sudo .venv/bin/pip install .
   ```

2. 安装已审核的生产分类文件：

   ```sh
   sudo install -d -o root -g root -m 0755 /etc/pnl-trader
   sudo install -o root -g root -m 0644 config/asset_classifications.toml /etc/pnl-trader/asset_classifications.toml
   ```

3. 创建 `/etc/pnl-trader/pnl-trader.env`，写入分类文件路径：

   ```ini
   PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml
   ```

   ```sh
   sudo install -o root -g root -m 0644 /dev/null /etc/pnl-trader/pnl-trader.env
   sudoedit /etc/pnl-trader/pnl-trader.env
   ```

   该环境文件只包含非敏感配置，保持 root 所有、权限 `0644`。

4. 创建 API 凭据文件并用安全编辑器写入密钥。文件须为 root 所有、权限 `0600`：

   - `/etc/pnl-trader/credentials/binance-api-key`
   - `/etc/pnl-trader/credentials/binance-api-secret`

   ```sh
   sudo install -d -o root -g root -m 0700 /etc/pnl-trader/credentials
   sudo install -o root -g root -m 0600 /dev/null /etc/pnl-trader/credentials/binance-api-key
   sudo install -o root -g root -m 0600 /dev/null /etc/pnl-trader/credentials/binance-api-secret
   sudoedit /etc/pnl-trader/credentials/binance-api-key
   sudoedit /etc/pnl-trader/credentials/binance-api-secret
   ```

   不要把密钥放进环境文件、命令行参数或 Git。

   Server酱通知是可选的。需要启用时，将 SendKey 写入 systemd credential store：

   ```sh
   sudo install -d -o root -g root -m 0700 /etc/credstore
   sudo install -o root -g root -m 0600 /dev/null /etc/credstore/serverchan_sendkey
   sudoedit /etc/credstore/serverchan_sendkey
   ```

   不配置该凭据时，服务仍可运行，但不会发送整轮失败通知。也可在本地运行时通过环境变量 `SERVERCHAN_SENDKEY` 配置。

5. 安装 unit 并执行预检：

   ```sh
   sudo install -o root -g root -m 0644 deploy/systemd/pnl-trader.service /etc/systemd/system/pnl-trader.service
   sudo systemctl daemon-reload
   sudo systemd-analyze verify /etc/systemd/system/pnl-trader.service
   sudo -u pnl-trader env \
     PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml \
     PNL_DATA_DIR=/var/lib/pnl-trader \
     /opt/pnl-accumulation-trading/.venv/bin/pnl-trader validate-config
   ```

`validate-config` 只检查本地配置和资产分类，不需要 Binance API 凭据。

预检通过后先停在这里，不要执行 `systemctl enable` 或 `systemctl start`。启动服务会连接主网并可能下单，须由项目负责人确认账户、现有持仓和资产分类后另行决定。
