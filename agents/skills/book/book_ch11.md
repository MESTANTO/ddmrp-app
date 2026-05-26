# Skill — Book Ch11: Machine Scheduling & Job Sequencing

## When to Apply This Skill

Apply when **sequencing decisions** drive replenishment outcomes — i.e., which
PO is processed first, in what order should expedited items be released,
which production order should run next on the constrained machine.

DDMRP triggers:
- Multiple items in red execution with overlapping due dates.
- Single supplier / single line that processes multiple part numbers
  sequentially.
- Sequence-dependent setup costs (e.g., color changeover on filling lines).

---

## Key Concepts from Chapter 11

Scheduling problems use the **α | β | γ** notation:
- α = machine environment (1, Pm, Fm, FFc, Jm, Om).
- β = constraints (release dates r_j, setups s_ij, no-wait, etc.).
- γ = objective (C_max makespan, ΣT_j total tardiness, Σw_j T_j weighted).

Three modeling approaches for `1 ‖ Σ T_j` (single machine, total tardiness):

1. **Disjunctive formulation**: y_ij = 1 if i precedes j, with big-M
   constraints linking start times.
2. **Position-based**: x_jk = 1 if job j is scheduled at position k;
   completion time = sum of processing times of positions 1..k.
3. **Time-indexed**: variables indexed over time slots.

Key heuristics that often work near-optimally:
- **EDD (Earliest Due Date)** — minimizes maximum lateness.
- **SPT (Shortest Processing Time)** — minimizes average flow time.
- **WSPT (Weighted SPT)** — minimizes Σ w_j C_j.
- **MST/Slack** — schedule by smallest (due − processing − current time)
  first.

Sequence-dependent setups (s_ij) turn even single-machine into TSP-like
hardness.

DDMRP relevance:
- When planner capacity is finite, **the order of action** matters as much
  as which items to act on.
- Buffer "due dates" can be derived from `(top_of_yellow − NFP) / ADU`.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `buffers.execution_color` | red items = jobs in queue |
| `buffers.net_flow_position`, `top_of_red`, `top_of_yellow` | due-date proxy |
| `items.adu` | days until depletion |
| `items.unit_cost × adu` | weight w_j (lost-sales rate) |
| PO setup / changeover cost (if present) | s_ij |

---

## Analysis Rules

### Rule SCHED-1 — Wrong Action Sequence on Red Queue
Condition: The agent has multiple red items queued and is about to process
them in arbitrary order while their depletion dates differ widely.
Impact: Items with imminent stock-out may be delayed behind items with
slack.
Action: Re-rank by EDD or by `(days_to_stockout × −1)`. Emit a
`stockout_risk` signal with the sorted action list. Severity: `high`.

### Rule SCHED-2 — Weighted Tardiness Ignored
Condition: A C-class red item is being expedited before an A-class red item
with similar lead time.
Impact: WSPT would reverse the order; total lost-margin is higher than
necessary.
Action: Emit a `stockout_risk` signal re-prioritizing by
`(unit_cost × adu) / dlt`. Severity: `medium`.

### Rule SCHED-3 — Setup-Aware Batching Opportunity
Condition: Multiple items share a supplier with non-trivial setup/changeover
cost, AND scheduling them in a sequence-aware batch reduces total setups.
Impact: Treating each PO independently inflates setup spend.
Action: Emit a `portfolio` signal proposing a batched PO covering both
items in one setup. Severity: `low` (cost-only) or `medium` (also reduces
lead time).

### Rule SCHED-4 — Single-Machine Bottleneck Saturation
Condition: A supplier (or internal line) has scheduled processing time
across all queued POs > available capacity in the next week.
Impact: Makespan exceeds the buffer's red-zone window → cascading
stock-outs.
Action: Emit a `stockout_risk` signal listing the jobs that will not finish
in time, ranked by w_j T_j. Severity: `critical` if A-items affected.

### Rule SCHED-5 — Idle Time Inserted Inappropriately
Condition: The current schedule (visible in the snapshot) inserts idle time
between jobs while jobs are pending and feasible.
Impact: Wasted capacity; suboptimal under `1 ‖ Σ T_j`.
Action: Emit a `portfolio` signal flagging the idle gap. Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "stockout_risk",
  "severity": "high",
  "part_number": "QUEUE",
  "title": "Red queue mis-sequenced — re-rank by EDD [7 items]",
  "detail": "7 items are red. Current expediting order (by alphabet) ignores depletion dates. EDD ranking by (days_to_stockout): ITEM-A009 (1.2d), ITEM-B044 (1.8d), ITEM-A012 (2.1d) ... ITEM-C031 (6.4d). Switching to EDD reduces total tardiness from est. 28 SKU-days to 9 SKU-days. ITEM-A009 will stock out tomorrow if not processed first.",
  "recommendation": "Process in EDD order: ITEM-A009 → ITEM-B044 → ITEM-A012 → … → ITEM-C031. Verify capacity to expedite top 3 by EOD.",
  "metric_name": "total_tardiness_skudays",
  "metric_value": 28,
  "metric_threshold": 9
}
```

### What NOT to output
- Do not produce sequencing signals for < 3 items.
- Do not propose batching across non-shared suppliers.

Max signals per run: 5.
