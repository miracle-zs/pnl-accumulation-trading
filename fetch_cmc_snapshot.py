"""
Fetch the CMC market-cap snapshot for 2026-01-01.

IMPORTANT: CoinMarketCap lists ~342 DUPLICATE tickers (meme/scam tokens reusing
real symbols: a rank-3646 "ETH" worth $13,041 vs the real rank-2 Ethereum at
$362B). A naive dict built by iterating the response lets the junk overwrite the
real coin, which silently corrupts any market-cap ranking.

Rule used here: for each symbol keep the entry with the LOWEST cmc_rank.

Output: data/cmc_20260101.json  {SYMBOL: {name, mcap, cmc_rank}}
"""

import json
from collections import Counter
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data"
DATE = "2026-01-01"


def main():
    key = ""
    for line in (Path(__file__).parent / ".env").read_text().splitlines():
        if line.startswith("CMC_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit("CMC_API_KEY not found in .env")

    res = requests.get("https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/historical",
                       headers={"X-CMC_PRO_API_KEY": key},
                       params={"date": DATE, "limit": 5000, "start": 1}, timeout=30)
    res.raise_for_status()
    rows = res.json().get("data", [])

    best, collisions = {}, {}
    for it in rows:
        sym = (it.get("symbol") or "").upper()
        mcap = ((it.get("quote") or {}).get("USD") or {}).get("market_cap")
        rank = it.get("cmc_rank")
        if not sym or not mcap or rank is None:
            continue
        if sym not in best or rank < best[sym]["cmc_rank"]:
            if sym in best:
                collisions.setdefault(sym, [best[sym]]).append(
                    {"name": it.get("name"), "mcap": mcap, "cmc_rank": rank})
            best[sym] = {"name": it.get("name"), "mcap": mcap, "cmc_rank": rank}
        else:
            collisions.setdefault(sym, []).append(
                {"name": it.get("name"), "mcap": mcap, "cmc_rank": rank})

    (DATA / "cmc_20260101.json").write_text(json.dumps(best, indent=1), encoding="utf-8")
    print(f"CMC {DATE}: {len(rows)} rows -> {len(best)} unique symbols kept")
    print(f"symbols with duplicate tickers: {len(collisions)}")
    for s in ["ETH", "BTC", "DASH", "RUNE", "ASTER", "STRK"]:
        if s in collisions:
            kept = best[s]
            print(f"  {s:<6} kept rank{kept['cmc_rank']} ${kept['mcap']:,.0f} {kept['name']}"
                  f"   (dropped {len(collisions[s])}: "
                  + ", ".join(f"{c['name']} ${c['mcap']:,.0f}" for c in collisions[s][:3]) + ")")
    print(f"saved -> {DATA / 'cmc_20260101.json'}")


if __name__ == "__main__":
    main()
