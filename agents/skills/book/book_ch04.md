# Skill — Book Ch4: Network Optimization (Flows & Routes)

## When to Apply This Skill

Apply when inventory decisions involve **flows between nodes** — multi-echelon
stocking, transshipment between plants/warehouses, or multi-period inventory
carrying represented as flow over a time-node network.

Trigger conditions:
- Snapshot includes multiple `location_id`s for the same item.
- Buffer placement at upstream vs. downstream nodes is in question (DDMRP
  decoupling-point decision).
- Replenishment can come from multiple warehouses (lateral transshipment).

---

## Key Concepts from Chapter 4

A network optimization problem is described by **nodes** (locations, time
periods, BOM components) and **arcs** (flows: shipments, time-carry,
production).

Core model is the **Minimum Cost Flow Problem (MCFP)**:

- Each arc carries a unit cost `c_ij` and capacity `k_ij`.
- Each node has supply, demand, or is transshipment (flow conservation:
  inflow = outflow).
- Objective: minimize total arc cost while balancing supply/demand.

Key property — **unimodularity**: a pure network LP always has integer
solutions when the inputs are integer. So you can solve a flow problem as an
LP and the answer will still be in whole units.

DDMRP-relevant insights:

1. **Decoupling-point placement** = choosing which nodes hold buffers. From a
   network view, every buffered node "absorbs" upstream variability, so the
   stock at downstream nodes can be smaller.
2. **Lateral transshipment** between warehouses is an arc with non-zero cost
   that can substitute for replenishment from the supplier when one node is
   red and another is green.
3. **Multi-period inventory** is naturally modeled as time-nodes connected by
   "carry" arcs (cost = holding cost per period).

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.location_id` (or `warehouses` join) | network nodes |
| `buffers.on_hand_qty` per location | supply at that node |
| `items.adu` per location | demand at that node |
| Inter-location shipping cost (if present) | arc cost |
| `items.dlt` | arc transit time |

---

## Analysis Rules

### Rule NET-1 — Lateral Transshipment Opportunity
Condition: Same `part_number` exists at two locations; one has
`execution_color = red` while the other has on-hand > top_of_yellow.
Impact: Stock-out risk at one location while excess sits at another. Naive
behavior is to expedite a new PO; a transshipment can be faster and cheaper.
Action: Emit a `stockout_risk` signal proposing lateral transfer, with
quantity = min(donor surplus above top_of_yellow, receiver gap to top_of_yellow).
Severity: `high`.

### Rule NET-2 — Decoupling Point Too Far Downstream
Condition: A buffered finished-good node has very high VF (variability factor
> 0.7) while its upstream parent in the BOM has stable demand.
Impact: Variability is being absorbed at the most expensive point in the
chain. Moving the decoupling point upstream reduces total inventory cost
under the same service level.
Action: Emit a `buffer_resizing` signal recommending evaluation of moving the
decoupling point one level upstream. Severity: `medium`.

### Rule NET-3 — Flow Imbalance (Supply ≠ Demand)
Condition: Aggregated supply across all sources for an item is < aggregated
ADU × DLT across all consuming nodes for the same item.
Impact: Network is fundamentally undersupplied; buffers will be in chronic
red regardless of resizing.
Action: Emit a `supplier_risk` signal calling for supplier capacity increase
or demand throttling. Severity: `critical` if the gap is > 20%.

### Rule NET-4 — Transshipment Arc Missing in BOM
Condition: Two warehouses serve overlapping customers but no inter-warehouse
lane is configured in the system.
Impact: Network has no relief valve; every shortage requires a fresh PO.
Action: Emit a `portfolio` signal recommending an inter-warehouse lane be set
up. Severity: `low`.

### Rule NET-5 — Holding-Cost-vs-Cycle-Stock Imbalance
Condition: An item carries on-hand ≥ 2 × green-zone TOG, while next upstream
node has high holding cost relative to downstream node (e.g., refrigerated vs
ambient).
Impact: Inventory is sitting at the most expensive carry-arc.
Action: Emit an `overstock` signal proposing carry-arc move (push stock
downstream or upstream depending on cost). Severity: `medium`.

---

## Output Format

```json
{
  "signal_type": "stockout_risk",
  "severity": "high",
  "part_number": "ITEM-N007",
  "title": "Lateral transshipment available — donor WH-B has surplus [ITEM-N007]",
  "detail": "ITEM-N007 at WH-A is in red (NFP 12% of TOR). WH-B holds 850 units, 320 above top_of_yellow. Transfer of 200 units brings WH-A to top_of_yellow without driving WH-B below TOY. Transshipment cost $0.45/unit << expediting cost from supplier.",
  "recommendation": "Initiate inter-warehouse transfer of 200 units WH-B → WH-A this week. Trigger only if no PO is en route to WH-A in next 5 days.",
  "metric_name": "lateral_transfer_qty",
  "metric_value": 200,
  "metric_threshold": 0
}
```

### What NOT to output
- Do not propose transshipment if both locations are red.
- Do not propose decoupling-point moves without naming both nodes.

Max signals per run: 8.
