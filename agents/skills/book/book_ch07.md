# Skill — Book Ch7: Supply Chain Network Design

## When to Apply This Skill

Apply this skill for **strategic, long-horizon decisions** about *where* to
hold inventory and *how many* stocking points to maintain. Triggered when:

- The snapshot covers multiple warehouses / DCs / plants.
- A "consolidate / open / close" facility question arises.
- Decoupling-point placement is being reconsidered at the network level
  (DDMRP strategic decision).

This skill is **strategic** (5–10-year horizon) — it should produce framing
signals, not weekly buffer actions.

---

## Key Concepts from Chapter 7

Three canonical models for network design:

1. **p-Median**: place exactly p facilities to minimize total
   demand-weighted distance. Customer assigned to exactly one facility.
2. **Uncapacitated Facility Location Problem (UFLP)**: optimize *how many*
   facilities (each with a fixed setup cost f_i), minimize fixed + transport
   cost. Single sourcing.
3. **Capacitated FLP (CFLP)**: same as UFLP but each facility has a
   service-capacity cap k_i.

All three use:
- Binary `y_i` = open facility i?
- Binary or continuous `x_ij` = serve customer j from facility i?
- Big-M linking: `x_ij ≤ y_i` (cannot serve from a closed facility).

Extensions:
- **Multi-tier** (suppliers → plants → DCs → retailers): add flow
  conservation at intermediate tiers (Ch4 carries over).
- **Multi-product**: index variables and constraints by product k.
- **Multi-modal**: add transport-mode binary variables (ocean/rail/truck).
- **Dynamic**: add time index t for staged build-out.

DDMRP-relevant insights:
- A new stocking point (decoupling point) has a **fixed setup cost** (system
  config, master-data effort) — UFLP-style trade-off.
- Closing a stocking point shifts variability downstream — the buffer at the
  remaining points must absorb more VF.
- The **demand-weighted distance** in p-median ≈ service-level proxy: closer
  stocking point = shorter customer DLT.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.location_id` (or warehouse master) | candidate facility set |
| Aggregated `adu` per location | demand at customer node |
| Inventory carrying cost per location | f_i (setup proxy) |
| Inter-node distance / transit (if present) | c_ij |

---

## Analysis Rules

### Rule SCND-1 — Underutilized Stocking Point
Condition: A warehouse holds < 5% of total network on-hand value AND serves
< 5% of total ADU.
Impact: Fixed overhead disproportionate to throughput. UFLP would likely
close it.
Action: Emit a `portfolio` signal proposing consolidation of this warehouse's
items into the nearest larger node, naming the receiver. Severity: `medium`.

### Rule SCND-2 — Customer Far From Nearest Stocking Point
Condition: A customer/region has demand-weighted distance to its serving
warehouse > 1.5× the network average.
Impact: Service level proxy is poor; p-median would assign elsewhere or open
a new node.
Action: Emit a `portfolio` signal flagging the gap. Severity: `low`.

### Rule SCND-3 — Capacity-Binding Facility
Condition: A warehouse is at > 90% of its physical/cube capacity, while
another node has < 50% utilization.
Impact: CFLP capacity constraint is binding; rebalance candidate.
Action: Emit a `portfolio` signal proposing item reallocation between nodes,
prioritizing slow-movers from the full node. Severity: `medium`.

### Rule SCND-4 — Decoupling Point Network Mismatch
Condition: Same item is buffered at multiple downstream nodes but not at the
upstream node feeding all of them.
Impact: Total network buffer = sum of downstream VF instead of pooled VF.
Pooling at upstream reduces total stock.
Action: Emit a `buffer_resizing` signal recommending decoupling-point
relocation upstream. Severity: `medium`.

### Rule SCND-5 — Single-Sourcing Strategic Risk
Condition: A high-value item is served from exactly one network node with no
backup.
Impact: One-node failure = full network stock-out.
Action: Emit a `supplier_risk` signal recommending mirror-stocking at a
second node. Severity: `high` for A-items, `medium` otherwise.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "NETWORK",
  "title": "Underutilized warehouse — consolidation candidate [WH-NORTH]",
  "detail": "WH-NORTH holds 3.2% of total on-hand value ($142K) and serves 4.1% of total ADU. Fixed overhead (estimated $180K/yr) exceeds the demand-weighted transport savings. UFLP-style analysis suggests closing WH-NORTH and serving its customers from WH-CENTRAL (avg distance increase: 180 mi).",
  "recommendation": "Initiate strategic review: 1) Quantify true fixed overhead at WH-NORTH. 2) Validate WH-CENTRAL can absorb +4% throughput. 3) If both confirm, propose 6-month consolidation plan.",
  "metric_name": "warehouse_value_share_pct",
  "metric_value": 3.2,
  "metric_threshold": 5.0
}
```

### What NOT to output
- Do not propose facility closures based on a single quarter's data.
- Do not emit network-design signals for single-warehouse snapshots.

Max signals per run: 5.
