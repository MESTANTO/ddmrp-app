# Skill — Book Ch2: Linear Programming for Inventory Mix

## When to Apply This Skill

Apply this skill when the agent must **allocate a continuous quantity across
multiple alternatives** under linear constraints — e.g., split demand across
multiple suppliers, allocate scarce stock to multiple customers, or distribute
production capacity across SKUs.

Trigger conditions in the snapshot:
- Two or more suppliers exist for the same purchased item (`items.item_type='P'`).
- A "Product Mix" or "Ingredient Mix" decision is implied.
- Capacity (warehouse, working capital, supplier ceiling) is binding.

---

## Key Concepts from Chapter 2

A Linear Program (LP) has the form:

```
min  c · x          (objective: linear in x)
s.t. A · x ≤ b       (constraints: linear)
     x ≥ 0          (non-negativity)
```

Three LP assumptions:
- **Proportionality** — cost/yield per unit is constant (no economies of scale).
- **Additivity** — total cost = sum of per-item costs (no interaction terms).
- **Divisibility** — variables can be fractional (no integer requirement).

Two canonical inventory uses:

1. **Product Mix** — given limited capacity and per-product margins, decide how
   many units of each SKU to produce/order. Maximize total margin subject to
   capacity and demand-cap constraints.
2. **Ingredient/Supplier Mix** — given a required total quantity and multiple
   suppliers with different costs, lead times, MOQs, decide allocation across
   suppliers to minimize total landed cost.

The LP solution often sits at a **vertex** of the feasible region — meaning
optimal allocations tend to be "all from supplier A and the rest from supplier
B" rather than spread thinly across many.

**Shadow prices / dual values** reveal which constraints are *binding*. A high
shadow price on a supplier's capacity constraint means relaxing it (negotiating
a higher cap) would have outsized financial value.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.unit_cost` | per-supplier landed cost |
| `items.moq` | min order constraint per supplier |
| `suppliers.lead_time_days` | timing constraint |
| `suppliers.reliability_score` | weight in objective when service-weighted |
| `items.adu × DLT × (1+LTF+VF)` | required total quantity for the cycle |

---

## Analysis Rules

### Rule LP-1 — Single-Supplier Concentration on High-Value Item
Condition: An A-class item (top 70% inventory value) is sourced 100% from a
single supplier and at least one alternative supplier exists in `suppliers`.
Impact: No allocation optimization is possible; supply risk is concentrated.
Action: Emit a `supplier_risk` signal proposing a 70/30 or 80/20 LP split,
naming both suppliers and the cost differential. Severity: `high`.

### Rule LP-2 — MOQ Forces Infeasibility
Condition: `items.moq > (adu × DLT × (1 + ltf + vf))` for a purchased item.
Impact: Minimum-buy alone fills more than one full buffer cycle → systematic
overstock. The LP is infeasible at the natural cycle quantity.
Action: Emit an `overstock` signal proposing (a) renegotiate MOQ down, or
(b) consolidate with another item from the same supplier. Severity: `medium`.

### Rule LP-3 — Capacity-Binding Item
Condition: An item's on-hand × unit_cost is a significant share of total
inventory value AND warehouse/budget capacity is referenced in the snapshot.
Impact: This item is the binding constraint — its shadow price is high.
Reducing its on-hand frees the most cash.
Action: Emit an `overstock` signal flagging this item as the priority for
right-sizing. Severity: `medium`.

### Rule LP-4 — Divisibility Violation Ignored
Condition: A recommendation produces a fractional order quantity (e.g., 17.4
pallets) on an item that ships only in whole units/pallets.
Impact: LP assumption of divisibility does not hold → see Chapter 3 (Integer
Programming) instead.
Action: Emit a `data_quality` signal asking the agent to re-frame as an ILP,
rounding to nearest pallet. Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "supplier_risk",
  "severity": "high",
  "part_number": "ITEM-A042",
  "title": "Single-supplier concentration on A-item — LP allocation possible [ITEM-A042]",
  "detail": "ITEM-A042 (A-class, 12% of inventory value) is 100% sourced from SUPP-1. SUPP-2 is qualified for this item with reliability 0.91 vs SUPP-1 at 0.87. A 70/30 split would reduce supply-disruption exposure with minimal cost impact (Δunit_cost = +1.8%).",
  "recommendation": "Allocate 70% to SUPP-1, 30% to SUPP-2 starting next replenishment cycle. Re-evaluate after 90 days. Owner: Procurement.",
  "metric_name": "supplier_concentration_pct",
  "metric_value": 100,
  "metric_threshold": 80
}
```

### What NOT to output
- Do not invent suppliers that don't exist in the snapshot.
- Do not propose splits that violate either supplier's MOQ.

Max signals per run: 8.
