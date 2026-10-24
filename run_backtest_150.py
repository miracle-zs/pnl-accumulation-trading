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
    universe_file = Path(__file__).parent / "data" / "universe_150_20260101.json"
    if not universe_file.exists():
        print("Universe file not found!")
        return
        
    universe = json.loads(universe_file.read_text())
    symbols = [x["symbol"] for x in universe]
    print(f"Loaded {len(symbols)} target symbols (150 Universe).")
    
    engine = BacktestEngine(
        initial_balance=1000.0,
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
    df_history.to_csv(RESULTS_DIR / "equity_curve_150.csv")
    if not df_trades.empty:
        df_trades.to_csv(RESULTS_DIR / "closed_trades_150.csv", index=False)
        
    # Trade statistics
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0]) if total_trades > 0 else 0
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
    total_realized_pnl = df_trades["pnl"].sum() if total_trades > 0 else 0.0
    
    tier_counts = df_trades["tier"].value_counts().to_dict() if total_trades > 0 else {}
    
    summary_150 = {
        "initial_capital": 1000.0,
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
    
    (RESULTS_DIR / "summary_150.json").write_text(json.dumps(summary_150, indent=2))
    
    # Check open positions
    open_tiers = {}
    total_open_notional = 0.0
    for pos in engine.positions.values():
        open_tiers[pos.tier] = open_tiers.get(pos.tier, 0) + 1
        total_open_notional += pos.size_notional
        
    # Compare with 100 symbols summary
    summary_100_file = RESULTS_DIR / "summary.json"
    summary_100 = json.loads(summary_100_file.read_text()) if summary_100_file.exists() else {}
    
    print("\n" + "="*70)
    print("             COMPARISON: 100 SYMBOLS vs 150 SYMBOLS             ")
    print("="*70)
    print(f"{'Metric':<25} | {'100 Symbols':<18} | {'150 Symbols':<18}")
    print("-"*70)
    print(f"{'Initial Capital':<25} | ${summary_100.get('initial_capital', 1000):<17,.2f} | ${summary_150['initial_capital']:<17,.2f}")
    print(f"{'Final Equity':<25} | ${summary_100.get('final_equity', 0):<17,.2f} | ${summary_150['final_equity']:<17,.2f}")
    print(f"{'Total Return':<25} | {((summary_100.get('final_equity', 1000)-1000)/10):<17.2f}% | {((summary_150['final_equity']-1000)/10):<17.2f}%")
    print(f"{'Lowest Equity':<25} | ${summary_100.get('lowest_equity', 0):<17,.2f} | ${summary_150['lowest_equity']:<17,.2f}")
    print(f"{'Max Drawdown':<25} | {summary_100.get('max_drawdown_pct', 0):<17.2f}% | {summary_150['max_drawdown_pct']:<17.2f}%")
    print(f"{'Total Closed Trades':<25} | {summary_100.get('total_closed_trades', 0):<18} | {summary_150['total_closed_trades']:<18}")
    print(f"{'Win Rate':<25} | {summary_100.get('win_rate_pct', 0):<17.2f}% | {summary_150['win_rate_pct']:<17.2f}%")
    print(f"{'Total Realized PnL':<25} | ${summary_100.get('total_realized_pnl', 0):<17,.2f} | ${summary_150['total_realized_pnl']:<17,.2f}")
    print(f"{'Closed Tier 1 / 2 / 3':<25} | {str(summary_100.get('tier_distribution', {})):<18} | {str(summary_150['tier_distribution']):<18}")
    print(f"{'Active Open Positions':<25} | 98                 | {len(engine.positions):<18}")
    print(f"{'Open Tiers (1 / 2 / 3)':<25} | {{1:27, 2:39, 3:32}} | {str(open_tiers):<18}")
    print(f"{'Open Total Notional':<25} | $3,375.00          | ${total_open_notional:<17,.2f}")
    print(f"{'Liquidation Status':<25} | {('BUST' if summary_100.get('is_liquidated') else 'SURVIVED'):<18} | {('BUST' if summary_150['is_liquidated'] else 'SURVIVED'):<18}")
    print("="*70)
    
    # Plot side-by-side comparison chart
    try:
        df_100 = pd.read_csv(RESULTS_DIR / "equity_curve.csv")
        df_100["timestamp"] = pd.to_datetime(df_100["timestamp"])
        df_100.set_index("timestamp", inplace=True)
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        
        ax1.plot(df_100.index, df_100["equity"], label="100 Symbols Equity", color="#2962FF", linewidth=1.5)
        ax1.plot(df_history.index, df_history["equity"], label="150 Symbols Equity", color="#FF6D00", linewidth=1.5)
        ax1.axhline(1000.0, color="#787B86", linestyle=":", label="Initial Capital ($1,000)")
        
        ax1.set_title("PnL Accumulation Strategy: 100 Symbols vs 150 Symbols (2026)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("Account Equity (USD)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        
        # Drawdown comparison
        df_100_peak = df_100["equity"].cummax()
        df_100_dd = (df_100["equity"] - df_100_peak) / df_100_peak * 100
        
        ax2.plot(df_100.index, df_100_dd, label="100 Symbols DD %", color="#2962FF", alpha=0.7)
        ax2.plot(df_history.index, df_history["drawdown"] * 100, label="150 Symbols DD %", color="#FF6D00", alpha=0.7)
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower left")
        
        plt.tight_layout()
        chart_path = RESULTS_DIR / "comparison_100_vs_150.png"
        plt.savefig(chart_path, dpi=200)
        plt.close()
        print(f"\nComparison chart saved to: {chart_path}")
    except Exception as e:
        print("Error saving comparison plot:", e)

if __name__ == "__main__":
    main()
