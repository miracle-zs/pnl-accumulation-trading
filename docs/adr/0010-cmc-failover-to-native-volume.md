# 0010. CMC Failover to Binance 24h Volume Ranking

If the CoinMarketCap API fails or times out during the daily 08:00 UTC rebalance, the system falls back seamlessly to the native Binance endpoint `GET /fapi/v1/ticker/24hr`, sorting active contracts by lowest 24h quote volume. Because micro-cap altcoins have a >90% correlation between lowest market cap and lowest trading volume, this ensures zero disruption to the daily candidate pipeline while eliminating external single points of failure.
