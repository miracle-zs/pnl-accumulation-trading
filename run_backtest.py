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
    universe_file = Path(__file__).parent / "data" / "initial_universe_20260101.json"
    if not universe_file.exists():
        print("Universe file not found!")
        return
        
    universe = json.loads(universe_file.read_text())
    symbols = [x["symbol"] for x in universe]
    print(f"Loaded {len(symbols)} target symbols.")
    
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
    df_history.to_csv(RESULTS_DIR / "equity_curve.csv")
    if not df_trades.empty:
        df_trades.to_csv(RESULTS_DIR / "closed_trades.csv", index=False)
        
    # Trade statistics
    total_trades = len(df_trades)
    winning_trades = len(df_trades[df_trades["pnl"] > 0]) if total_trades > 0 else 0
    win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
    total_realized_pnl = df_trades["pnl"].sum() if total_trades > 0 else 0.0
    
    tier_counts = df_trades["tier"].value_counts().to_dict() if total_trades > 0 else {}
    
    summary = {
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
    
    (RESULTS_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    
    # Print formatted summary report
    print("\n" + "="*60)
    print("           PNL ACCUMULATION BACKTEST REPORT           ")
    print("="*60)
    print(f"Period:                {start_date} ~ {end_date}")
    print(f"Initial Capital:       $1,000.00")
    print(f"Final Equity:          ${final_equity:,.2f}")
    print(f"Peak Equity:           ${peak_equity:,.2f}")
    print(f"Lowest Equity:         ${lowest_equity:,.2f}")
    print(f"Max Drawdown:          {max_drawdown*100:.2f}%")
    print(f"Total Closed Trades:   {total_trades}")
    print(f"Win Rate:              {win_rate:.2f}%")
    print(f"Total Realized Profit: ${total_realized_pnl:,.2f}")
    print(f"Tier Distribution:     {tier_counts}")
    print(f"Liquidation Status:    {'[!] LIQUIDATED (BUST)' if engine.is_liquidated else 'SURVIVED'}")
    if engine.is_liquidated:
        print(f"Liquidation Timestamp: {engine.liquidation_time}")
        print(f"Liquidation Reason:    {engine.liquidation_reason}")
    print("="*60)
    
    # Plot performance chart
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
        
        # Equity Curve
        ax1.plot(df_history.index, df_history["equity"], label="Account Equity", color="#2962FF", linewidth=1.5)
        ax1.plot(df_history.index, df_history["cash_balance"], label="Cash Balance", color="#00E676", linestyle="--", alpha=0.7)
        ax1.axhline(1000.0, color="#787B86", linestyle=":", label="Initial Capital ($1,000)")
        
        if engine.is_liquidated and engine.liquidation_time:
            ax1.axvline(engine.liquidation_time, color="#FF1744", linestyle="-", label="Liquidation Triggered")
            ax1.scatter([engine.liquidation_time], [0], color="#FF1744", s=100, zorder=5)
            
        ax1.set_title("PnL Accumulation Strategy: Equity & Cash Curve (2026)", fontsize=14, fontweight="bold")
        ax1.set_ylabel("USD ($)", fontsize=12)
        ax1.legend(loc="upper left")
        ax1.grid(True, alpha=0.3)
        
        # Drawdown Chart
        ax2.fill_between(df_history.index, df_history["drawdown"] * 100, 0, color="#FF5252", alpha=0.5, label="Drawdown %")
        ax2.set_ylabel("Drawdown (%)", fontsize=12)
        ax2.set_xlabel("Date", fontsize=12)
        ax2.grid(True, alpha=0.3)
        ax2.legend(loc="lower left")
        
        plt.tight_layout()
        chart_path = RESULTS_DIR / "backtest_chart.png"
        plt.savefig(chart_path, dpi=200)
        plt.close()
        print(f"\nPerformance chart saved to: {chart_path}")
    except Exception as e:
        print("Error saving plot:", e)

if __name__ == "__main__":
    main()
