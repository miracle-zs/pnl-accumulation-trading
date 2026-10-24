import os
import json
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from engine import BacktestEngine

RESULTS_DIR = Path(__file__).parent / "data" / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

def main():
    universe_file = Path(__file__).parent / "data" / "universe_full_20260101.json"
    if not universe_file.exists():
        print("Universe file not found!")
        return
        
    universe = json.loads(universe_file.read_text())
    symbols = [x["symbol"] for x in universe]
    print(f"Loaded {len(symbols)} target symbols (Full Universe with $1,000 Capital & 5U Notional).")
    
    engine = BacktestEngine(
        initial_balance=1000.0,
        initial_notional=5.0,
        leverage=10.0,
        maker_fee=0.0002,
        taker_fee=0.0005,
        maintenance_margin_rate=0.01
    )
    
    engine.load_data(symbols)
    
    start_date = "2026-01-01 00:00:00"
    end_date = "2026-09-30 23:00:00"
    
    engine.run_backtest(start_date=start_date, end_date=end_date)
    
    # Analyze results
    df_history = pd.DataFrame(engine.history)
    df_trades = pd.DataFrame(engine.closed_trades)
    
    if df_history.empty:
        print("No history recorded.")
        return
        
    df_history.set_index("timestamp", inplace=True)
    df_history["peak_equity"] = df_history["equity"].cummax()
    df_history["drawdown"] = (df_history["equity"] - df_history["peak_equity"]) / df_history["peak_equity"]
    
    max_drawdown = df_history["drawdown"].min()
    peak_equity = df_history["equity"].max()
    final_equity = df_history["equity"].iloc[-1]
    lowest_equity = df_history["equity"].min()
    
    # Save CSVs
    df_history.to_csv(RESULTS_DIR / "equity_curve_full_5u.csv")
    if not df_trades.empty:
        df_trades.to_csv(RESULTS_DIR / "closed_trades_full_5u.csv", index=False)
        
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0]) if total_trades > 0 else 0
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
    total_realized_pnl = df_trades["pnl"].sum() if total_trades > 0 else 0.0
    
    tier_counts = df_trades["tier"].value_counts().to_dict() if total_trades > 0 else {}
    
    summary_5u = {
        "initial_capital": 1000.0,
        "initial_notional": 5.0,
        "final_equity": float(final_equity),
        "peak_equity": float(peak_equity),
        "lowest_equity": float(lowest_equity),
        "max_drawdown_pct": float(max_drawdown * 100),
        "is_liquidated": engine.is_liquidated,
        "liquidation_time": str(engine.liquidation_time) if engine.liquidation_time else None,
        "liquidation_reason": engine.liquidation_reason,
        "total_closed_trades": total_trades,
        "win_rate_pct": float(win_rate),
        "total_realized_pnl": float(total_realized_pnl),
        "tier_distribution": tier_counts
    }
    
    (RESULTS_DIR / "summary_full_5u.json").write_text(json.dumps(summary_5u, indent=2))
    
    open_tiers = {}
    total_open_notional = 0.0
    for pos in engine.positions.values():
        open_tiers[pos.tier] = open_tiers.get(pos.tier, 0) + 1
        total_open_notional += pos.size_notional
        
    summary_1000u_10u = json.loads((RESULTS_DIR / "summary_full.json").read_text())
    summary_2000u_10u = json.loads((RESULTS_DIR / "summary_full_2000u.json").read_text())
    
    print("\n" + "="*95)
    print("      FULL UNIVERSE: 1,000U (10U) vs 2,000U (10U) vs 1,000U (5U) COMPARISON      ")
    print("="*95)
    print(f"{'Metric':<25} | {'1,000U (10U/单)':<18} | {'2,000U (10U/单)':<18} | {'1,000U (5U/单)':<18}")
    print("-"*95)
    print(f"{'Initial Capital':<25} | ${summary_1000u_10u['initial_capital']:<17,.2f} | ${summary_2000u_10u['initial_capital']:<17,.2f} | ${summary_5u['initial_capital']:<17,.2f}")
    print(f"{'Initial Real Leverage':<25} | 4.53x              | 2.27x              | {((453*5)/1000):<17.2f}x")
    print(f"{'Final Equity':<25} | ${summary_1000u_10u['final_equity']:<17,.2f} | ${summary_2000u_10u['final_equity']:<17,.2f} | ${summary_5u['final_equity']:<17,.2f}")
    print(f"{'Total Return':<25} | {((summary_1000u_10u['final_equity']-1000)/10):<17.2f}% | {((summary_2000u_10u['final_equity']-2000)/20):<17.2f}% | {((summary_5u['final_equity']-1000)/10):<17.2f}%")
    print(f"{'Lowest Equity':<25} | ${summary_1000u_10u['lowest_equity']:<17,.2f} | ${summary_2000u_10u['lowest_equity']:<17,.2f} | ${summary_5u['lowest_equity']:<17,.2f}")
    print(f"{'Max Drawdown':<25} | {summary_1000u_10u['max_drawdown_pct']:<17.2f}% | {summary_2000u_10u['max_drawdown_pct']:<17.2f}% | {summary_5u['max_drawdown_pct']:<17.2f}%")
    print(f"{'Total Closed Trades':<25} | {summary_1000u_10u['total_closed_trades']:<18} | {summary_2000u_10u['total_closed_trades']:<18} | {summary_5u['total_closed_trades']:<18}")
    print(f"{'Total Realized PnL':<25} | ${summary_1000u_10u['total_realized_pnl']:<17,.2f} | ${summary_2000u_10u['total_realized_pnl']:<17,.2f} | ${summary_5u['total_realized_pnl']:<17,.2f}")
    print(f"{'Active Open Positions':<25} | {summary_1000u_10u.get('active_positions', 452):<18} | {summary_2000u_10u.get('active_positions', 455):<18} | {len(engine.positions):<18}")
    print(f"{'Open Total Notional':<25} | $7,170.00          | $13,045.00         | ${total_open_notional:<17,.2f}")
    print(f"{'Liquidation Status':<25} | {('BUST' if summary_1000u_10u['is_liquidated'] else 'SURVIVED'):<18} | {('BUST' if summary_2000u_10u['is_liquidated'] else 'SURVIVED'):<18} | {('BUST' if summary_5u['is_liquidated'] else 'SURVIVED'):<18}")
    print("="*95)
    
    # Plot comparison chart
    try:
        df_1000_10u = pd.read_csv(RESULTS_DIR / "equity_curve_full.csv")
        df_1000_10u["timestamp"] = pd.to_datetime(df_1000_10u["timestamp"])
        df_1000_10u.set_index("timestamp", inplace=True)
        
        df_2000_10u = pd.read_csv(RESULTS_DIR / "equity_curve_full_2000u.csv")
        df_2000_10u["timestamp"] = pd.to_datetime(df_2000_10u["timestamp"])
        df_2000_10u.set_index("timestamp", inplace=True)
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        
        ax1.plot(df_1000_10u.index, df_1000_10u["equity"], label="1,000U Cap (10U/单) [BUST]", color="#D50000", linewidth=1.2, linestyle="--")
        ax1.plot(df_2000_10u.index, df_2000_10u["equity"], label="2,000U Cap (10U/单)", color="#00C853", linewidth=1.5, alpha=0.7)
        ax1.plot(df_history.index, df_history["equity"], label="1,000U Cap (5U/单) [NEW]", color="#2962FF", linewidth=2.0)
        ax1.axhline(1000.0, color="#787B86", linestyle=":", label="Initial Capital ($1,000)")
        
        ax1.set_title("Full Universe: 1,000U (10U) vs 2,000U (10U) vs 1,000U (5U) (2026)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("Account Equity (USD)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        
        ax2.plot(df_history.index, df_history["drawdown"] * 100, label="1,000U (5U) Drawdown %", color="#2962FF", alpha=0.8)
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower left")
        
        plt.tight_layout()
        chart_path = RESULTS_DIR / "comparison_full_5u.png"
        plt.savefig(chart_path, dpi=200)
        plt.close()
        print(f"\nComparison chart saved to: {chart_path}")
    except Exception as e:
        print("Error saving plot:", e)

if __name__ == "__main__":
    main()
