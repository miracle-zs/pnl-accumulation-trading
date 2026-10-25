import json
from pathlib import Path
import numpy as np
import simulate_instant_reopen as SIR

info = json.loads(Path("data/bars_info_full.json").read_text())
symbols = info["symbols"]

r_next = SIR.run_simulation("next_bar")
r_inst = SIR.run_simulation("intra_bar_instant")

print("--- Realized vs Floating PnL ---")
print(f"Next-bar (1h delay): Realized ${r_next['total_realized_cash']:,.2f}, Floating ${r_next['final_floating_pnl']:,.2f}, Net ${r_next['total_net_profit']:,.2f}")
print(f"Instant (秒开):       Realized ${r_inst['total_realized_cash']:,.2f}, Floating ${r_inst['final_floating_pnl']:,.2f}, Net ${r_inst['total_net_profit']:,.2f}")

diff_net = r_next["sym_net_total"] - r_inst["sym_net_total"]
worst_for_inst = np.argsort(diff_net)[-10:]

print("\n--- Top 10 Symbols where Next-bar beat Instant the most ---")
for idx in worst_for_inst[::-1]:
    sym = symbols[idx]
    n_p = r_next["sym_net_total"][idx]
    i_p = r_inst["sym_net_total"][idx]
    print(f"{sym:<16}: Next ${n_p:>8.2f} vs Instant ${i_p:>8.2f} (Diff: +${diff_net[idx]:>6.2f})")

best_for_inst = np.argsort(diff_net)[:10]
print("\n--- Top 10 Symbols where Instant beat Next-bar the most ---")
for idx in best_for_inst:
    sym = symbols[idx]
    n_p = r_next["sym_net_total"][idx]
    i_p = r_inst["sym_net_total"][idx]
    print(f"{sym:<16}: Next ${n_p:>8.2f} vs Instant ${i_p:>8.2f} (Diff: -${-diff_net[idx]:>6.2f})")
