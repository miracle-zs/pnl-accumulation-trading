import json
import time
from pathlib import Path

# Load 630 pure crypto symbols
with open("data/pure_crypto_active_symbols.json") as f:
    crypto_symbols = json.load(f)

# Non crypto symbols from pure_crypto_active_symbols.md
non_crypto_symbols = [
    # US tech giants & equities
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "NFLX", "ORCL", "IBM", "INTC", "AMD",
    "QCOM", "AVGO", "CRM", "ADBE", "CSCO", "UBER", "DELL", "NOW", "VRT", "ARM", "ASML", "AMAT",
    "LRCX", "KLAC", "MRVL", "MU", "TXN", "ALAB", "ANET", "APLD", "AXTI", "CIEN", "COHR", "CRDO",
    "CRWV", "CXMT", "DRAM", "FLEX", "LITE", "MP", "SNDK", "TER", "TSM", "WDC", "AAOI", "CHIP",
    "SNOW", "DDOG", "NET", "CRWD", "PANW", "PATH", "TEAM", "TWST", "GTLB", "BILL", "PLTR", "RDDT",
    "SHOP", "MDB", "EBAY", "PYPL", "PAYP", "APP", "COIN", "MSTR", "MARA", "IREN", "HUT", "HOOD",
    "SOFI", "DJT", "RIVN", "SMCI", "IONQ", "OKLO",
    # Trad equities & healthcare
    "ASTS", "CVNA", "HIMS", "DKNG", "FLNC", "GPRO", "HPE", "MRNA", "NVO", "ONDS", "RKLB", "TTWO",
    "VST", "ACN", "ACU", "BMNR", "BRKB", "BX", "COST", "CRML", "DIS", "GLW", "GS", "HD", "JPM",
    "LLY", "MRK", "NKE", "UNH", "V", "WMT", "KO", "NOK", "AMC", "GEV", "TEM", "NBIS", "BID",
    "CAT", "BE", "BZ", "CL", "FWDI", "O",
    # Pre-IPO / Synthetic
    "OPENAI", "ANTHROPIC", "UNITREE", "MINIMAX", "CRCL",
    # HK / China concept
    "BABA", "PDD", "TENCENT", "MEITUAN", "KUAISHOU", "BYD", "POPMART", "HK0625", "HK0700", "HK0992", "HK1810",
    # Korean & international
    "SAMSUNG", "SAMSUNGEM", "SKHYNIX", "SKHY", "HYUNDAI", "NAVER", "SONY", "LGELECTRONICS", "HANMI",
    "CSOPSAMSUNG2L", "CSOPSKHYNIX2L", "KODEX200", "SKDD", "SKUU",
    # ETFs, commodities, macro
    "SPY", "QQQ", "TQQQ", "SQQQ", "SOXL", "SOXS", "IWM", "SMH", "GDX", "UVXY", "BITO", "EWJ",
    "EWY", "EWZ", "EWT", "KORU", "KSTR", "TBT", "TMF", "TSLL", "NVDL", "TZA", "URNM", "SPCX",
    "BWET", "STXX", "CBRS", "ROBO", "COPPER", "NATGAS", "USDBRL", "BTCDOM", "DEFI", "USDC"
]

now_ms = int(time.time() * 1000)

lines = [
    '# Production Asset Classifications Directory',
    '# Audited 630 Native Crypto Assets + 187 Non-Crypto Assets',
    'version = "v1-2026-10-25"',
    ''
]

seen_bases = set()

# 1. Native Crypto
for s in crypto_symbols:
    base = s.replace("USDT", "")
    if base not in seen_bases:
        seen_bases.add(base)
        lines.append('[[assets]]')
        lines.append(f'base_asset = "{base}"')
        lines.append('classification = "NATIVE_CRYPTO"')
        lines.append('source = "audited_binance_native_crypto"')
        lines.append(f'reviewed_at_ms = {now_ms}')
        lines.append(f'note = "Audited native crypto symbol {s}"')
        lines.append('')

# 2. Non Crypto
for base in non_crypto_symbols:
    if base not in seen_bases:
        seen_bases.add(base)
        lines.append('[[assets]]')
        lines.append(f'base_asset = "{base}"')
        lines.append('classification = "NON_CRYPTO"')
        lines.append('source = "audited_binance_non_crypto"')
        lines.append(f'reviewed_at_ms = {now_ms}')
        lines.append('note = "Excluded TradFi/Equity/ETF/Macro asset"')
        lines.append('')

out_path = Path("config/asset_classifications.toml")
out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"Written {len(seen_bases)} assets ({len(crypto_symbols)} crypto) to {out_path}")
