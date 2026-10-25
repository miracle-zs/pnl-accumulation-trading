import os
import json
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

from engine import BacktestEngine

RESULTS_DIR = Path(__file__).resolve().parent.parent / "data" / "results"
RESULTS_DIR.mkdir(exist_ok=True, parents=True)

def main():
    universe_file = Path(__file__).resolve().parent.parent / "data" / "universe_full_20260101.json"
    if not universe_file.exists():
        print("Universe file not found!")
        return
        
    universe = json.loads(universe_file.read_text())
    symbols = [x["symbol"] for x in universe]
    print(f"Loaded {len(symbols)} target symbols (Full Universe with $2,000 Capital).")
    
    engine = BacktestEngine(
        initial_balance=2000.0,
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
    df_history.to_csv(RESULTS_DIR / "equity_curve_full_2000u.csv")
    if not df_trades.empty:
        df_trades.to_csv(RESULTS_DIR / "closed_trades_full_2000u.csv", index=False)
        
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0]) if total_trades > 0 else 0
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
    total_realized_pnl = df_trades["pnl"].sum() if total_trades > 0 else 0.0
    
    tier_counts = df_trades["tier"].value_counts().to_dict() if total_trades > 0 else {}
    
    summary_2000u = {
        "initial_capital": 2000.0,
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
    
    (RESULTS_DIR / "summary_full_2000u.json").write_text(json.dumps(summary_2000u, indent=2))
    
    open_tiers = {}
    total_open_notional = 0.0
    for pos in engine.positions.values():
        open_tiers[pos.tier] = open_tiers.get(pos.tier, 0) + 1
        total_open_notional += pos.size_notional
        
    summary_1000u = json.loads((RESULTS_DIR / "summary_full.json").read_text())
    
    print("\n" + "="*80)
    print("      FULL UNIVERSE (472 SYMBOLS): $1,000 vs $2,000 CAPITAL COMPARISON      ")
    print("="*80)
    print(f"{'Metric':<25} | {'1,000U Capital':<22} | {'2,000U Capital':<22}")
    print("-"*80)
    print(f"{'Initial Capital':<25} | ${summary_1000u['initial_capital']:<21,.2f} | ${summary_2000u['initial_capital']:<21,.2f}")
    print(f"{'Initial Leverage':<25} | 4.53x                  | 2.27x                 ")
    print(f"{'Final Equity':<25} | ${summary_1000u['final_equity']:<21,.2f} | ${summary_2000u['final_equity']:<21,.2f}")
    print(f"{'Total Return':<25} | {((summary_1000u['final_equity']-1000)/10):<21.2f}% | {((summary_2000u['final_equity']-2000)/20):<21.2f}%")
    print(f"{'Lowest Equity':<25} | ${summary_1000u['lowest_equity']:<21,.2f} | ${summary_2000u['lowest_equity']:<21,.2f}")
    print(f"{'Max Drawdown':<25} | {summary_1000u['max_drawdown_pct']:<21.2f}% | {summary_2000u['max_drawdown_pct']:<21.2f}%")
    print(f"{'Total Closed Trades':<25} | {summary_1000u['total_closed_trades']:<22} | {summary_2000u['total_closed_trades']:<22}")
    print(f"{'Total Realized PnL':<25} | ${summary_1000u['total_realized_pnl']:<21,.2f} | ${summary_2000u['total_realized_pnl']:<21,.2f}")
    print(f"{'Active Open Positions':<25} | {summary_1000u.get('active_positions', 452):<22} | {len(engine.positions):<22}")
    print(f"{'Open Total Notional':<25} | $7,170.00              | ${total_open_notional:<21,.2f}")
    print(f"{'Liquidation Status':<25} | {('BUST' if summary_1000u['is_liquidated'] else 'SURVIVED'):<22} | {('BUST' if summary_2000u['is_liquidated'] else 'SURVIVED'):<22}")
    if summary_2000u['is_liquidated']:
        print(f"{'Liquidation Time':<25} | {summary_1000u['liquidation_time']:<22} | {summary_2000u['liquidation_time']:<22}")
    print("="*80)
    
    # Plot comparison chart
    try:
        df_1000 = pd.read_csv(RESULTS_DIR / "equity_curve_full.csv")
        df_1000["timestamp"] = pd.to_datetime(df_1000["timestamp"])
        df_1000.set_index("timestamp", inplace=True)
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(13, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        
        ax1.plot(df_1000.index, df_1000["equity"], label="Full Universe (1,000U Capital)", color="#D50000", linewidth=1.5, linestyle="--")
        ax1.plot(df_history.index, df_history["equity"], label="Full Universe (2,000U Capital)", color="#00C853", linewidth=2.0)
        ax1.axhline(2000.0, color="#787B86", linestyle=":", label="Initial Capital ($2,000)")
        
        ax1.set_title("Full Universe: $1,000 Capital (Bust) vs $2,000 Capital (2026)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("Account Equity (USD)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        
        ax2.plot(df_history.index, df_history["drawdown"] * 100, label="2,000U Drawdown %", color="#00C853", alpha=0.8)
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower left")
        
        plt.tight_layout()
        chart_path = RESULTS_DIR / "comparison_full_1000_vs_2000.png"
        plt.savefig(chart_path, dpi=200)
        plt.close()
        print(f"\nComparison chart saved to: {chart_path}")
    except Exception as e:
        print("Error saving plot:", e)

if __name__ == "__main__":
    main()
