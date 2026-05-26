# Skill — Book Ch10: Supply Chain Configuration & Decoupling Point

## When to Apply This Skill

Apply when the agent must reason about **where to place the
push/pull boundary** (decoupling point) and **how much safety stock to hold
at which BOM level** — the core strategic insight of DDMRP framed as an
optimization problem.

Trigger conditions:
- Items span multiple BOM levels (raw → component → semi-finished → FG).
- A trade-off between MTS, ATO, MTO, ETO paradigms is implied.
- Total network safety-stock cost is high relative to revenue.

This is the chapter that **directly maps to the DDMRP decoupling-point
concept** — read it as DDMRP's strategic counterpart.

---

## Key Concepts from Chapter 10

**Push vs Pull**:
- Pure push: produce to forecast, safety stock at the **end** of the chain
  (finished goods). Efficient for low-variety, high-volume.
- Pure pull: produce to order, safety stock at the **start** (raw materials).
  Lean, flexible, high-variety.

The optimum is usually **hybrid** — split by a **decoupling point (DP)**:

| Paradigm | DP location | Volume | Variety | Examples |
|---|---|---|---|---|
| MTS  | Finished goods       | High    | Low       | Diapers, paper |
| ATS  | Semi-finished        | Med-high| Medium    | Canned food, mowers |
| ATO  | Semi-finished        | Medium  | Med-high  | Computers, cameras |
| MTO  | Components           | Med-low | High      | Tooling, equipment |
| ETO  | Raw materials        | Low     | High      | Aircraft, ambulance |

**Safety Stock Placement Problem (SSPP)**:

For a BOM network G(V,A) with demand ~ N(μ, σ²) and lead time T_i per node:

- Each node has an inbound and outbound service time.
- Net replenishment time τ_i = (inbound + lead time) − outbound.
- Safety stock at node i ∝ z · σ · √τ_i (z = service-level factor).
- Objective: minimize total holding cost subject to a customer-quoted
  outbound service-time cap.

Result: holding **all** stock at finished goods or **all** at raw materials
is rarely optimal; the optimum interleaves stocking nodes along the BOM.

This is the analytical justification for the DDMRP buffer-placement
methodology.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.item_type` (M/I/P/D) | BOM-level identification |
| BOM relationships | network arcs |
| `items.dlt` | T_i (lead time at node) |
| Demand mean/std (computed) | μ, σ |
| `items.unit_cost` × holding rate | per-node holding cost |
| Quoted customer lead time | outbound service-time cap |

---

## Analysis Rules

### Rule CONFIG-1 — Decoupling at Wrong Tier
Condition: A high-variety finished good (e.g., > 8 SKU variants from same
parent component) is buffered as MTS while the upstream component is not
buffered.
Impact: Holding safety stock at FG-level for high-variety = expensive
(redundant stock per variant). Moving DP upstream to the component (ATO)
typically halves total safety-stock cost.
Action: Emit a `buffer_resizing` signal recommending DP move upstream.
Severity: `medium`.

### Rule CONFIG-2 — Engineered Item Holding FG Stock
Condition: An item whose demand is highly intermittent (sporadic, low ADU
with high σ) is buffered at finished-goods level.
Impact: ETO/MTO would be appropriate; the buffer ties up cash on uncertain
demand.
Action: Emit an `overstock` signal recommending shift to MTO with raw-material
stock only. Severity: `medium`.

### Rule CONFIG-3 — Service-Time Promise Tighter Than Lead Time
Condition: Quoted customer lead time < sum of supplier + production lead
times AND no FG buffer exists.
Impact: Service promise infeasible without finished-goods stock.
Action: Emit a `stockout_risk` signal proposing either FG buffer creation
or service-time renegotiation. Severity: `high`.

### Rule CONFIG-4 — Safety-Stock Concentration Imbalance
Condition: > 80% of total safety-stock value sits at a single BOM level (FG
or raw material).
Impact: Sub-optimal placement; SSPP would distribute differently.
Action: Emit a `buffer_resizing` signal proposing redistribution. Severity:
`low`.

### Rule CONFIG-5 — Variety Without Postponement
Condition: A family of high-variant items shares > 80% of upstream BOM but
all variants are independently buffered.
Impact: Postponement (build-to-order at last differentiation point) would
reduce total stock dramatically.
Action: Emit a `portfolio` signal proposing postponement re-design.
Severity: `medium`.

---

## Output Format

```json
{
  "signal_type": "buffer_resizing",
  "severity": "medium",
  "part_number": "ITEM-FG-FAMILY",
  "title": "DP at FG level on high-variety family — move upstream [12 SKUs]",
  "detail": "12 finished-good variants share component ITEM-COMP-099. All 12 are buffered at FG level (combined safety stock $186K). SSPP analysis: moving DP to ITEM-COMP-099 (buffer only the component, build FG to order) reduces total safety stock to estimated $74K (60% reduction) while meeting customer-quoted lead time of 5d (component receipt 2d + FG assembly 2d).",
  "recommendation": "(1) Establish buffer at ITEM-COMP-099 sized for combined demand. (2) Phase out individual FG buffers over 90d. (3) Validate FG assembly capacity supports 2-day MTO turn.",
  "metric_name": "safety_stock_value",
  "metric_value": 186000,
  "metric_threshold": 74000
}
```

### What NOT to output
- Do not propose DP moves without BOM data in the snapshot.
- Do not recommend MTO/ETO conversions on commodity high-volume items.

Max signals per run: 6.
