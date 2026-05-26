# Skill — Book Ch8: Production Planning (MPS / MRP / Lot-Sizing)

## When to Apply This Skill

Apply when the agent must reason about **how much and when to produce/order
across multiple time periods**, balancing setup costs, variable production
costs, and inventory holding costs. Especially relevant for:

- Items with item_type = `M` (Manufactured) or `P` with batch-setup cost.
- Multi-period demand visible in the snapshot.
- BOM-driven dependent demand (MRP cascade).

This skill complements DDMRP's day-by-day execution view with the
tactical 6-month – 2-year planning view.

---

## Key Concepts from Chapter 8

**Lot-sizing problem (LS-U)** — single-item, uncapacitated:

```
min Σ_t (q_t y_t + c_t x_t + h_t I_t)
s.t. I_{t-1} + x_t = I_t + d_t        (flow conservation)
     x_t ≤ M y_t                       (big-M: no production unless setup)
     I_0 = 0
     x_t, I_t ≥ 0; y_t ∈ {0,1}
```

where x_t = lot size, y_t = setup indicator, I_t = ending inventory.

Three classical extensions:
1. **Master Production Scheduling (MPS)** = multi-item, capacitated LS. Adds
   resource capacity constraint `Σ_i α_ik x_it ≤ L_kt`. Decisions: how to
   allocate scarce resources across competing SKUs.
2. **Material Requirements Planning (MRP)** = takes MPS output + BOM + lead
   times → derives required quantities for sub-components.
3. **MRP II** = MRP with explicit capacity constraints at every BOM level.

Key trade-off: large lot-size → fewer setups (lower q_t y_t) but more
holding (higher h_t I_t). The Wagner-Whitin policy says: produce in period
t only enough to cover demand through some future period j, never more.

DDMRP-relevant translation:
- The **green zone** ≈ economic order quantity in lot-sizing.
- The **red zone** ≈ safety component of multi-period planning.
- DDMRP de-emphasizes deterministic multi-period schedules in favor of
  buffer-driven pull, but **the planning view still matters for
  manufactured items** where setup costs are significant.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.item_type` = M | manufactured / batched item |
| `items.moq` | proxy for production lot size |
| Production setup cost (if present) | q_t |
| `items.unit_cost × holding_rate` | h_t |
| Multi-period `demand_entries` | d_t |

---

## Analysis Rules

### Rule PROD-1 — Excessive Setup Frequency
Condition: A manufactured item is produced in > 80% of recent time periods
(daily/weekly buckets) AND average lot-size < MOQ × 2.
Impact: Setup cost dominates total cost; classic over-frequent batching.
Action: Emit an `overstock` signal (or `portfolio` framing) proposing
larger lot-sizes covering 2–3 demand periods. Severity: `medium`.

### Rule PROD-2 — Setup Cost Not Captured in Green Zone
Condition: A manufactured item has DDMRP green zone < typical setup-driven
EOQ.
Impact: Green-zone replenishment cycles too small → frequent setups → cost
inflation.
Action: Emit a `buffer_resizing` signal proposing larger order multiplier
(OM) that aligns with setup economics. Severity: `medium`.

### Rule PROD-3 — Resource Capacity Bottleneck
Condition: Across a planning bucket, the implied production for buffered
M-items exceeds available capacity (resource utilization > 95%).
Impact: MRP II infeasibility — buffers cannot be replenished as planned.
Action: Emit a `stockout_risk` signal listing the items that will lose to
the higher-priority allocation. Severity: `high`.

### Rule PROD-4 — BOM Lead-Time Stack Exceeds Planning Window
Condition: Sum of lead times along a BOM path > planning horizon in the
snapshot.
Impact: MRP cannot fully plan parent items; lower-level components are
ordered "blind".
Action: Emit a `data_quality` signal proposing extended planning horizon or
buffer at intermediate BOM level. Severity: `medium`.

### Rule PROD-5 — Holding-Cost Anomaly
Condition: An item's average on-hand inventory × holding rate > setup cost ×
average setups per year by a factor of 3 or more.
Impact: Lot-sizing skewed: producing too much per setup.
Action: Emit an `overstock` signal proposing lot-size reduction. Severity:
`medium`.

---

## Output Format

```json
{
  "signal_type": "buffer_resizing",
  "severity": "medium",
  "part_number": "ITEM-M044",
  "title": "Green zone undersized vs setup economics [ITEM-M044]",
  "detail": "ITEM-M044 (manufactured, setup cost $480, holding $0.12/unit/day) has DDMRP green zone of 120 units, implying ~14 replenishments/yr ($6,720 in setup). Lot-sizing EOQ ≈ 340 units (~5 replenishments/yr, $2,400). Net annual savings if green zone aligned with EOQ: $3,200 setup, +$1,100 holding → $2,100 net.",
  "recommendation": "Raise green-zone order multiplier so cycle quantity ≈ 340 units. Re-evaluate after 90 days vs actual setup frequency.",
  "metric_name": "green_zone_qty",
  "metric_value": 120,
  "metric_threshold": 340
}
```

### What NOT to output
- Do not propose lot-size changes for purely make-to-stock C-items with
  trivial setup costs.
- Do not override DDMRP buffer logic for I- or D-type items (intermediate /
  distributed).

Max signals per run: 6.
