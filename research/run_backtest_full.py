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
    print(f"Loaded {len(symbols)} target symbols (Full Universe).")
    
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
    df_history.to_csv(RESULTS_DIR / "equity_curve_full.csv")
    if not df_trades.empty:
        df_trades.to_csv(RESULTS_DIR / "closed_trades_full.csv", index=False)
        
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0]) if total_trades > 0 else 0
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
    total_realized_pnl = df_trades["pnl"].sum() if total_trades > 0 else 0.0
    
    tier_counts = df_trades["tier"].value_counts().to_dict() if total_trades > 0 else {}
    
    summary_full = {
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
    
    (RESULTS_DIR / "summary_full.json").write_text(json.dumps(summary_full, indent=2))
    
    open_tiers = {}
    total_open_notional = 0.0
    for pos in engine.positions.values():
        open_tiers[pos.tier] = open_tiers.get(pos.tier, 0) + 1
        total_open_notional += pos.size_notional
        
    summary_100 = json.loads((RESULTS_DIR / "summary.json").read_text())
    summary_150 = json.loads((RESULTS_DIR / "summary_150.json").read_text())
    summary_200 = json.loads((RESULTS_DIR / "summary_200.json").read_text())
    
    print("\n" + "="*95)
    print("             COMPARISON: 100 vs 150 vs 200 vs FULL UNIVERSE (472)             ")
    print("="*95)
    print(f"{'Metric':<24} | {'100 Symbols':<14} | {'150 Symbols':<14} | {'200 Symbols':<14} | {'Full (~472)':<14}")
    print("-"*95)
    print(f"{'Initial Capital':<24} | ${summary_100['initial_capital']:<13,.2f} | ${summary_150['initial_capital']:<13,.2f} | ${summary_200['initial_capital']:<13,.2f} | ${summary_full['initial_capital']:<13,.2f}")
    print(f"{'Final Equity':<24} | ${summary_100['final_equity']:<13,.2f} | ${summary_150['final_equity']:<13,.2f} | ${summary_200['final_equity']:<13,.2f} | ${summary_full['final_equity']:<13,.2f}")
    print(f"{'Total Return':<24} | {((summary_100['final_equity']-1000)/10):<13.2f}% | {((summary_150['final_equity']-1000)/10):<13.2f}% | {((summary_200['final_equity']-1000)/10):<13.2f}% | {((summary_full['final_equity']-1000)/10):<13.2f}%")
    print(f"{'Lowest Equity':<24} | ${summary_100['lowest_equity']:<13,.2f} | ${summary_150['lowest_equity']:<13,.2f} | ${summary_200['lowest_equity']:<13,.2f} | ${summary_full['lowest_equity']:<13,.2f}")
    print(f"{'Max Drawdown':<24} | {summary_100['max_drawdown_pct']:<13.2f}% | {summary_150['max_drawdown_pct']:<13.2f}% | {summary_200['max_drawdown_pct']:<13.2f}% | {summary_full['max_drawdown_pct']:<13.2f}%")
    print(f"{'Total Closed Trades':<24} | {summary_100['total_closed_trades']:<14} | {summary_150['total_closed_trades']:<14} | {summary_200['total_closed_trades']:<14} | {summary_full['total_closed_trades']:<14}")
    print(f"{'Total Realized PnL':<24} | ${summary_100['total_realized_pnl']:<13,.2f} | ${summary_150['total_realized_pnl']:<13,.2f} | ${summary_200['total_realized_pnl']:<13,.2f} | ${summary_full['total_realized_pnl']:<13,.2f}")
    print(f"{'Active Open Positions':<24} | 98             | 146            | 195            | {len(engine.positions):<14}")
    print(f"{'Open Total Notional':<24} | $3,375.00      | $4,945.00      | $6,105.00      | ${total_open_notional:<13,.2f}")
    print(f"{'Liquidation Status':<24} | {('BUST' if summary_100['is_liquidated'] else 'SURVIVED'):<14} | {('BUST' if summary_150['is_liquidated'] else 'SURVIVED'):<14} | {('BUST' if summary_200['is_liquidated'] else 'SURVIVED'):<14} | {('BUST' if summary_full['is_liquidated'] else 'SURVIVED'):<14}")
    if summary_full['is_liquidated']:
        print(f"{'Liquidation Time':<24} | {'N/A':<14} | {'N/A':<14} | {'N/A':<14} | {summary_full['liquidation_time']:<14}")
    print("="*95)
    
    # Plot 4-curve comparison
    try:
        df_100 = pd.read_csv(RESULTS_DIR / "equity_curve.csv")
        df_100["timestamp"] = pd.to_datetime(df_100["timestamp"])
        df_100.set_index("timestamp", inplace=True)
        
        df_150 = pd.read_csv(RESULTS_DIR / "equity_curve_150.csv")
        df_150["timestamp"] = pd.to_datetime(df_150["timestamp"])
        df_150.set_index("timestamp", inplace=True)
        
        df_200 = pd.read_csv(RESULTS_DIR / "equity_curve_200.csv")
        df_200["timestamp"] = pd.to_datetime(df_200["timestamp"])
        df_200.set_index("timestamp", inplace=True)
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        
        ax1.plot(df_100.index, df_100["equity"], label="100 Symbols", color="#2962FF", linewidth=1.2, alpha=0.7)
        ax1.plot(df_150.index, df_150["equity"], label="150 Symbols", color="#FF6D00", linewidth=1.2, alpha=0.7)
        ax1.plot(df_200.index, df_200["equity"], label="200 Symbols", color="#AA00FF", linewidth=1.2, alpha=0.7)
        ax1.plot(df_history.index, df_history["equity"], label="Full Universe (472)", color="#D50000", linewidth=2.0)
        ax1.axhline(1000.0, color="#787B86", linestyle=":", label="Initial Capital ($1,000)")
        
        if engine.is_liquidated and engine.liquidation_time:
            ax1.axvline(engine.liquidation_time, color="#D50000", linestyle="--", label=f"Liquidated at {engine.liquidation_time}")
            
        ax1.set_title("PnL Accumulation Strategy: 100 vs 150 vs 200 vs Full Universe (2026)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("Account Equity (USD)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        
        # Drawdowns
        df_100_dd = (df_100["equity"] - df_100["equity"].cummax()) / df_100["equity"].cummax() * 100
        df_150_dd = (df_150["equity"] - df_150["equity"].cummax()) / df_150["equity"].cummax() * 100
        df_200_dd = (df_200["equity"] - df_200["equity"].cummax()) / df_200["equity"].cummax() * 100
        
        ax2.plot(df_100.index, df_100_dd, label="100 DD %", color="#2962FF", alpha=0.5)
        ax2.plot(df_150.index, df_150_dd, label="150 DD %", color="#FF6D00", alpha=0.5)
        ax2.plot(df_200.index, df_200_dd, label="200 DD %", color="#AA00FF", alpha=0.5)
        ax2.plot(df_history.index, df_history["drawdown"] * 100, label="Full Universe DD %", color="#D50000", alpha=0.8)
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower left")
        
        plt.tight_layout()
        chart_path = RESULTS_DIR / "comparison_all_universes.png"
        plt.savefig(chart_path, dpi=200)
        plt.close()
        print(f"\n4-Way comparison chart saved to: {chart_path}")
    except Exception as e:
        print("Error saving 4-way comparison plot:", e)

if __name__ == "__main__":
    main()
