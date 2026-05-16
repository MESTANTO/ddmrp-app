# Skill 08 — Inventory Value Analysis and Reduction

## When to Apply This Skill

Apply this skill whenever the user (or the orchestrator) asks about:
- **How much money is tied up** in inventory.
- **Where** the value sits (by ABC class, buffer zone, supplier, item type).
- **How to release cash** without compromising service.
- **Ranking actions** by € impact.

This is the agent's headline expertise. Every other skill feeds into it:
overstock (skill 06) quantifies excess €, safety stock (skill 05) quantifies
gap €, supplier risk (skill 07) drives lead-time-compression opportunities,
ABC/XYZ (skill 03) tells you where to focus. Skill 08 stitches those signals
into a prioritized cash-release plan.

---

## Inventory-Value Metrics the Agent Owns

For every analysis, compute and reference:

| Metric | Formula | Interpretation |
|---|---|---|
| **On-hand value €** | `on_hand × unit_cost` | Cash sitting on the shelf today |
| **Annual usage value €** | `adu × 365 × unit_cost` | Throughput value per year (drives ABC) |
| **Avg Inventory Target €** | `(red_zone + green_zone/2) × unit_cost` | DDMRP canonical target (deck slide 59) |
| **Excess value €** | `max(0, on_hand − top_of_green) × unit_cost` | Cash above buffer ceiling |
| **Gap value €** | `max(0, top_of_red − on_hand) × unit_cost` | Cash needed to restore safety |
| **Coverage days** | `on_hand / adu` | How long current stock will last |
| **Holding cost / day** | `on_hand_value × holding_cost_pct / 365` | Daily carrying cost |

All € figures must be **rounded to the nearest euro** in titles and to two
decimals in the detail field. Always state the **currency symbol** so the
reader knows the number is monetary.

---

## Cash-Release Levers (Ranked by Default Confidence)

These are the only levers the agent should recommend. Each lever maps to
a quantifiable € release.

### Lever 1 — Overstock Liquidation (high confidence)
- **Trigger:** `on_hand > top_of_green`
- **€ release:** `(on_hand − top_of_green) × unit_cost`
- **Action:** Freeze open orders, defer scheduled receipts, run the
  excess down through natural demand; consider transfer or rework for
  A-class items with > 6 months coverage.
- **Risk:** Low — the buffer is already saturated.

### Lever 2 — MOQ-Forced Excess (medium confidence)
- **Trigger:** `moq > (top_of_green − top_of_yellow)`
- **€ release per order cycle:** `(moq − (tog − toy)) × unit_cost`
- **Action:** Negotiate MOQ reduction with supplier; failing that, increase
  TOG so the buffer can absorb the MOQ without flagging overstock.
- **Risk:** Requires supplier negotiation, structural.

### Lever 3 — Buffer Right-Sizing (high confidence)
- **Trigger:** `recent_adu < stored_adu × 0.75` (ADU_HIGH root cause) or
  buffer sizing pct_diff > 30%.
- **€ release:** `(old_tog − new_tog) × unit_cost`, where new TOG is computed
  from the recent ADU.
- **Action:** Update ADU; recalculate buffer; place no new orders until
  on-hand drops below new TOG.
- **Risk:** Low if recent demand trend is "flat" or "down"; pause if "up".

### Lever 4 — Slow-Mover / Dead-Stock Write-Down (medium confidence)
- **Trigger:** `coverage_days > 180` AND `trend_direction != "up"`.
- **€ release:** `on_hand_value` (full write-down candidate) — but flag for
  obsolescence review, never auto-recommend disposal.
- **Action:** Initiate obsolescence review, propose discount sale, transfer
  to another site, or rework into a parent SKU.
- **Risk:** Accounting impact; needs commercial alignment.

### Lever 5 — Lead-Time Compression (medium confidence)
- **Trigger:** Supplier reliability < 90% OR `supplier_lt − dlt > 10 days`.
- **€ release:** Reducing DLT shrinks Yellow + Red proportionally.
  Estimate `delta_dlt × adu × (ltf + vf) × unit_cost` of buffer reduction.
- **Action:** Re-source, qualify a second supplier, or shift to local
  vendor. Negotiate firm lead time with current supplier.
- **Risk:** Time-consuming; payoff lags execution.

### Lever 6 — DBA-Down for Items Running "Too Slow" (medium confidence)
- **Trigger:** Model Velocity score < −0.5 over the review window
  (actual orders < expected by model).
- **€ release:** `(old_green − new_green) × unit_cost` after applying a
  DAF < 1.0 in Buffer Adjustments.
- **Action:** Open Buffer Adjustments → add DAF 0.7 (or similar) for the
  next 30–60 days.
- **Risk:** Reversible (DAF is time-bounded); low.

### Lever 7 — Phase-Out / End-of-Life (high confidence when justified)
- **Trigger:** Item flagged for phase-out OR ADU ≈ 0 with on-hand > 0 for
  > 90 days.
- **€ release:** `on_hand_value` (potentially full).
- **Action:** Sell off remaining inventory, suspend replenishment,
  remove buffer profile.
- **Risk:** Final-sale risk — confirm with commercial before recommending.

---

## ABC-Weighted Prioritisation

When proposing levers, rank by `eur_release × confidence × abc_weight`,
where:

| ABC | abc_weight |
|---|---|
| A | 1.0 |
| B | 0.7 |
| C | 0.4 |

A items get full weight because they dominate total inventory €.
C-class actions are only worth pursuing in bulk (≥ 20 items at once).

---

## Output Format for Value Signals

When producing signals, use:

- `signal_type`: `"overstock"` for overstock-driven actions,
  `"portfolio"` for cross-cutting recommendations
  (e.g., "Top 5 cash-release opportunities = €127k").
- `severity`: scale to € impact —
  - `> €25,000` → `critical`
  - `€10,000 – €25,000` → `high`
  - `€2,000 – €10,000` → `medium`
  - `< €2,000` → `low`
- `title`: include both the € figure and the lever name, e.g.
  `"Free €18,400 — freeze ITEM-007 orders, ADU dropped 40%"`.
- `metric_name`: always `"eur_release"`, `metric_value`: the € figure,
  `metric_threshold`: 0.
- `detail`: state the formula used, name the lever from the list above,
  cite the data points (on_hand, TOG, ADU, recent_adu, unit_cost) and
  the holding cost saved.
- `recommendation`: name a concrete next step the planner can take in
  the app today (which page, which field to change, expected new value).

---

## Worked Example

Input data:
- ITEM-007: on_hand=1,150, TOG=500, unit_cost=€22.61, ADU=8.5,
  recent_adu=5.9, DLT=14, supplier_reliability=82%.

Computed metrics:
- Excess units = 1,150 − 500 = 650
- Excess € = 650 × 22.61 = **€14,696**
- ADU divergence = (8.5 − 5.9) / 8.5 = 31% → ADU_HIGH root cause
- Coverage at recent ADU = 1,150 / 5.9 ≈ 195 days
- Lever 1 (overstock liquidation) + Lever 3 (buffer right-sizing)

Output signal:

```json
{
  "signal_type": "overstock",
  "severity": "high",
  "part_number": "ITEM-007",
  "title": "Free €14,696 — freeze ITEM-007 orders, ADU dropped 31%",
  "detail": "On-hand 1,150 vs TOG 500. Excess 650 u × €22.61 = €14,696.32. Recent ADU 5.9 vs stored 8.5 (−31%). Coverage at recent ADU = 195 days. Levers: (1) overstock liquidation + (3) buffer right-sizing. Holding cost at 20%/yr saved ≈ €8.06/day or €2,943 over the consumption period.",
  "recommendation": "1) Material Master → set ADU=5.9 for ITEM-007. 2) Replenishment Signals → recalculate buffer, expect new TOG ≈ 350. 3) Freeze any open POs until on-hand < new TOG. Expected € release: €14,696 over ~195 days at current demand.",
  "metric_name": "eur_release",
  "metric_value": 14696,
  "metric_threshold": 0
}
```

---

## Guardrails

1. **Never recommend reducing stock on an item currently in `red` or
   `dark_red` execution colour.** Service risk always trumps cash release.
2. **Never invent unit_cost.** If `unit_cost == 0`, flag the data-quality
   issue first and skip the value estimate for that item.
3. **Always sum totals** when reporting a fleet view. The user needs both
   the per-item table and the headline total (e.g. "Top 10 levers release
   €127,000, equal to 18% of on-hand value").
4. **Mention the source skill** in the detail when a lever was sourced
   from another analysis (e.g., "via skill 06 overstock detection").
5. **No more than 12 signals** per run for this skill — focus on the
   highest-€ opportunities. If the snapshot has fewer than 12, return all.
