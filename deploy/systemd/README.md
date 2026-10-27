# Linux systemd 部署

`pnl-trader.service` 连接 Binance USDⓈ-M Futures 主网。启动后会运行巡检，并可能提交真实市价单；当前工作区没有安装或启动该服务。

不带子命令运行 `pnl-trader` 只显示帮助。只有显式执行 `pnl-trader run` 才会启动主网交易服务；systemd unit 也使用该命令。

## 安装与预检（不启动）

目标主机需运行 Linux、systemd 和 Python 3.11 或更新版本，并安装 Git、`venv` 和 `pip`。以下步骤只安装和验证，不启用或启动服务。

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

3. 在项目根目录创建 `.env`，写入运行配置：

   ```ini
   PNL_ASSET_CLASSIFICATIONS=/etc/pnl-trader/asset_classifications.toml
   PNL_DATA_DIR=/var/lib/pnl-trader
   BINANCE_API_KEY=
   BINANCE_API_SECRET=
   SERVERCHAN_SENDKEY=
   ```

   ```sh
   sudo install -o root -g root -m 0600 /dev/null /opt/pnl-accumulation-trading/.env
   sudoedit /opt/pnl-accumulation-trading/.env
   ```

   `.env` 由 systemd 读取，须保持 root 所有、权限 `0600`。该文件已被 `.gitignore` 排除；不要提交到 Git。Server酱是可选项，填写 `SERVERCHAN_SENDKEY` 后才会发送整轮失败提醒。

4. 安装 unit 并执行预检：

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

## 公网只读看板

看板独立于交易服务运行，仅读取 `/var/lib/pnl-trader/state.sqlite3`，监听服务器本机 `127.0.0.1:8001`。公网地址为 `https://43.167.191.253/pnl-trader/`，无需密码。HTTPS 根路径会跳转到项目路径。

服务器的 80 端口由另一项目使用，保留其现有路由。为签发和续期 IP 证书，只在该默认 HTTP server 中增加以下 ACME challenge location：

```nginx
location ^~ /.well-known/acme-challenge/ {
    root /var/www/pnl-acme;
    default_type text/plain;
    try_files $uri =404;
}
```

安装 Certbot 5.4 或更新版本；IP 证书须采用 Let's Encrypt `shortlived` profile，证书有效期约 160 小时，因此必须保留 Certbot 自动续期 timer。首次签发和续期配置参考：

```sh
sudo snap install certbot --classic
sudo install -d -o root -g root -m 0755 /var/www/pnl-acme/.well-known/acme-challenge
sudo certbot certonly --webroot --webroot-path /var/www/pnl-acme \
  --preferred-profile shortlived --ip-address 43.167.191.253 \
  --agree-tos --register-unsafely-without-email --non-interactive
sudo certbot reconfigure --cert-name 43.167.191.253 \
  --preferred-profile shortlived --webroot --webroot-path /var/www/pnl-acme \
  --ip-address 43.167.191.253 --deploy-hook 'systemctl reload nginx'
```

签发成功后安装并启用两个 unit：

```sh
sudo install -o root -g root -m 0644 deploy/systemd/pnl-trader-dashboard.service /etc/systemd/system/pnl-trader-dashboard.service
sudo install -o root -g root -m 0644 deploy/nginx/pnl-trader-dashboard.conf /etc/nginx/sites-available/pnl-trader-dashboard
sudo ln -s /etc/nginx/sites-available/pnl-trader-dashboard /etc/nginx/sites-enabled/pnl-trader-dashboard
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable --now pnl-trader-dashboard.service
sudo systemctl reload nginx
```

需确保云防火墙放行 TCP 443。IP 证书和 Nginx TLS 配置依据 [Let's Encrypt 的 Certbot IP 证书说明](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html)。
