# Skill — Book Ch3: Integer Programming for Discrete Inventory Decisions

## When to Apply This Skill

Apply when decisions are **discrete** — counts of pallets, full truckloads,
batches, or yes/no choices (open a supplier, include an SKU in a kit, expedite
or not). Triggered by:

- Items whose MOQ or pack-size is ≥ 25% of a typical replenishment quantity.
- Decisions framed as "should we…" with a binary outcome.
- Knapsack-style problems: pick a subset of items to expedite under a budget.

---

## Key Concepts from Chapter 3

Integer Programming (IP) restricts decision variables to integers, often binary
{0,1}. Three common forms in inventory:

1. **Binary choice** — x∈{0,1}. Example: open supplier S? expedite item I?
2. **Knapsack** — pick a subset of items maximizing benefit subject to a
   resource cap (budget, truck capacity).
3. **Set covering** — choose a minimum set of suppliers/warehouses such that
   every demand point is "covered" by at least one.

**Big-M constraints**: linking a binary variable y to a continuous quantity x:
`x ≤ M · y`. If y=0 then x=0 (supplier not opened → no orders). M must be
large enough not to artificially bind, but small enough to keep the LP
relaxation tight.

**Branch-and-bound** is the standard solution method — solve the LP relaxation,
branch on a fractional variable, prune by bound. Practical takeaway: integer
programs can be **dramatically harder** than LPs. Adding a binary variable
roughly doubles the search tree.

**Cutting planes** strengthen the formulation by adding valid inequalities
that exclude fractional solutions without excluding integer ones.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.moq` | discrete batch size |
| `items.pack_size` (if present) | ship multiples |
| `items.unit_cost` | knapsack value coefficient |
| `buffers.execution_color` | which items need expediting |
| Snapshot `cash_envelope` or `budget` | knapsack capacity |

---

## Analysis Rules

### Rule IP-1 — Indivisible Pack-Size Coercion
Condition: An order recommendation produced by buffer math is not a multiple
of `items.moq` or known pack-size.
Impact: Order is impractical (can't ship 17.4 pallets).
Action: Round UP to the next valid multiple. If rounding adds ≥ 30% over the
green-zone quantity → emit `overstock` signal labeling this an
"MOQ-driven overstock" candidate. Severity: `medium`.

### Rule IP-2 — Expediting Knapsack
Condition: Two or more items are in `red` or `dark_red` execution color AND
the snapshot includes a stated expediting budget/cap.
Impact: Naive "expedite all red" exceeds budget; some items must be skipped.
Action: Rank red items by `(stock-out cost per day × days short)` ÷
`expediting cost`. Recommend expediting the top-K that fit the budget. Emit a
`stockout_risk` signal listing the ranked subset. Severity: `high`.

### Rule IP-3 — Supplier On/Off Decision
Condition: A supplier has activity in < 3 items and average annual spend below
a meaningful threshold (e.g., 1% of total spend).
Impact: Per-supplier fixed cost (audits, EDI setup, payment cycles) may exceed
the value of keeping the supplier active. Binary decision: keep or drop.
Action: Emit a `supplier_risk` signal recommending consolidation onto a larger
supplier. Severity: `low` if no service impact, `medium` otherwise.

### Rule IP-4 — Set-Covering Gap
Condition: A demand region/warehouse has no supplier within an acceptable lead
time, while at least one other supplier *could* serve it with a small
qualification cost.
Impact: Service exposure; one supplier outage creates an uncovered region.
Action: Emit a `supplier_risk` signal suggesting the cheapest set-cover
extension. Severity: `medium`.

### Rule IP-5 — Big-M Misuse Warning (Modeling Hygiene)
Condition: The agent has just proposed a constraint of the form "if y=0 then
order = 0" without specifying the M bound.
Impact: Recommendation is mathematically loose and may permit infeasible
intermediate states.
Action: Emit a `data_quality` signal requesting the M bound be tightened to
`max(adu × DLT × 2)` for that item. Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "stockout_risk",
  "severity": "high",
  "part_number": "ITEM-R007",
  "title": "Expediting knapsack — top 3 of 7 red items fit budget [ITEM-R007 + 2 others]",
  "detail": "7 items in red execution. Expediting budget = $25,000. Ranking by (lost-margin per day × days short)/expediting cost: ITEM-R007 (score 8.4), ITEM-R012 (6.2), ITEM-R044 (5.9) consume $23,400 and cover 71% of total exposure. Remaining 4 items should be addressed by buffer resizing.",
  "recommendation": "Expedite ITEM-R007, ITEM-R012, ITEM-R044 this week. For the other 4, run buffer_resizing skill.",
  "metric_name": "items_within_budget",
  "metric_value": 3,
  "metric_threshold": 7
}
```

### What NOT to output
- Do not produce signals for items whose MOQ has not been verified.
- Do not propose expediting without a budget reference.

Max signals per run: 8.
