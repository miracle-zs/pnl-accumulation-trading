import os
import json
import xml.etree.ElementTree as ET
from pathlib import Path
import requests

def load_cmc_key():
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("CMC_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.getenv("CMC_API_KEY", "")

def get_binance_usdt_symbols():
    url = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/klines/"
    res = requests.get(url, timeout=15)
    root = ET.fromstring(res.content)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    prefixes = [cp.find("s3:Prefix", ns).text for cp in root.findall("s3:CommonPrefixes", ns)]
    binance_symbols = {p.replace("data/futures/um/monthly/klines/", "").rstrip("/") for p in prefixes if p.endswith("USDT/")}
    return binance_symbols

def get_cmc_listings_for_date(api_key, date_str, limit=1000):
    url = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/historical"
    headers = {"X-CMC_PRO_API_KEY": api_key}
    params = {"date": date_str, "limit": limit, "start": 1}
    res = requests.get(url, headers=headers, params=params, timeout=20)
    res.raise_for_status()
    return res.json().get("data", [])

def get_bottom_100_symbols(date_str="2026-01-01"):
    api_key = load_cmc_key()
    if not api_key:
        raise ValueError("CMC_API_KEY not found in .env")
    
    binance_symbols = get_binance_usdt_symbols()
    cmc_items = get_cmc_listings_for_date(api_key, date_str, limit=1000)
    
    matched = []
    for item in cmc_items:
        sym = item.get("symbol", "").upper()
        quote = item.get("quote", {}).get("USD", {})
        mcap = quote.get("market_cap")
        if not mcap or mcap <= 0:
            continue
        
        pair = f"{sym}USDT"
        if pair in binance_symbols:
            matched.append({
                "symbol": pair,
                "base_asset": sym,
                "name": item.get("name"),
                "mcap": mcap,
                "cmc_rank": item.get("cmc_rank")
            })
            continue
        
        pair_1000 = f"1000{sym}USDT"
        if pair_1000 in binance_symbols:
            matched.append({
                "symbol": pair_1000,
                "base_asset": sym,
                "name": item.get("name"),
                "mcap": mcap,
                "cmc_rank": item.get("cmc_rank")
            })
            
    # Sort ascending by market cap
    matched.sort(key=lambda x: x["mcap"])
    return matched[:100]

if __name__ == "__main__":
    print("Fetching bottom 100 contracts for 2026-01-01...")
    bottom_100 = get_bottom_100_symbols("2026-01-01")
    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "initial_universe_20260101.json"
    out_file.write_text(json.dumps(bottom_100, indent=2))
    print(f"Successfully saved {len(bottom_100)} symbols to {out_file}")
    min_mcap = bottom_100[0]['mcap']
    max_mcap = bottom_100[-1]['mcap']
    print(f"Market Cap Range: ${min_mcap:,.0f} ~ ${max_mcap:,.0f}")
    print("First 10:", [x["symbol"] for x in bottom_100[:10]])
    print("Last 10:", [x["symbol"] for x in bottom_100[-10:]])
