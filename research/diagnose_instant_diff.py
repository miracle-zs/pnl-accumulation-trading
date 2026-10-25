import json
from pathlib import Path
import numpy as np
import simulate_instant_reopen as SIR

info = json.loads(Path("data/bars_info_full.json").read_text())
symbols = info["symbols"]

print("Running next_bar...")
r_next = SIR.run_simulation("next_bar")
print("Running intra_bar_instant...")
r_inst = SIR.run_simulation("intra_bar_instant")

print("\n--- Summary ---")
print(f"Next-bar: Opens {r_next['total_opens']}, TPs {r_next['total_tps']}, Realized ${r_next['total_realized_cash']:,.2f}, Net ${r_next['total_net_profit']:,.2f}")
print(f"Instant:  Opens {r_inst['total_opens']}, TPs {r_inst['total_tps']}, Realized ${r_inst['total_realized_cash']:,.2f}, Net ${r_inst['total_net_profit']:,.2f}")

diff = r_next["sym_net_total"] - r_inst["sym_net_total"]
top_diff_idx = np.argsort(diff)[-5:]

print("\nTop 5 symbols where 1h delay beat Instant:")
for idx in top_diff_idx[::-1]:
    sym = symbols[idx]
    p_next = r_next["sym_net_total"][idx]
    p_inst = r_inst["sym_net_total"][idx]
    print(f"  {sym:<16}: Next-bar ${p_next:>8.2f} vs Instant ${p_inst:>8.2f} (Diff: +${diff[idx]:>6.2f})")

# Let's inspect the top winning symbol diff specifically
top_sym = symbols[top_diff_idx[-1]]
print(f"\nAnalyzing trade breakdown for {top_sym}...")
