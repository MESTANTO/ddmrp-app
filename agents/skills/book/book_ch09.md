# Skill — Book Ch9: Resource Planning (Workforce & Capacity)

## When to Apply This Skill

Apply when the limiting factor on replenishment or stocking is **not stock
itself but a resource** — labor (warehouse staff, planners), receiving
capacity, expediting bandwidth, or supplier-side production capacity.

In a DDMRP app, the workforce angle most often shows up as:
- Number of POs/items a planner can manage per day → impacts how many red
  alerts can realistically be acted on.
- Receiving-dock throughput → limits how fast buffers can be replenished.
- Skill-mix at suppliers → some POs need specialized handling.

---

## Key Concepts from Chapter 9

Resource Planning (RP) mirrors production planning but for **people and
capacity**:

- **Labor Strategy Optimization (LSO)** — strategic mix of internal/contractor
  workforce to meet revenue targets. Uses a **Bill of Labor (BOL)** —
  analogous to BOM, but resource items decompose target revenue into labor
  hours via weighted arcs `h_ij(·)`.
- **Project Portfolio Optimization (PPO)** — tactical selection of projects
  subject to budget and labor capacity.
- **Resource Matching & Assignment** — operational pairing of personnel with
  tasks.

Key constructs:
- `h_ij(·)` arc weights represent productivity, risk-discounts (offshore
  λ-factor), efficiency (μ = billable/total hours).
- Resource types: internal (`V_I`), outsourced (`V_O`), demand nodes (`V_M`).
- Risk factors discount nominal capacity.

DDMRP translation:
- Plan buffer-execution actions against the **finite planner capacity**.
- When too many items go red simultaneously, this is a planner-throughput
  problem, not a stock problem.

---

## Data Source in This App

| Field | Used for |
|---|---|
| Count of red/dark_red items | demand for planner actions |
| Planner team size (snapshot or user-provided) | resource capacity |
| Average actions per day per planner | productivity multiplier |
| Number of suppliers per planner | span of control |

---

## Analysis Rules

### Rule RP-1 — Planner Overload
Condition: Count of items in `red` or `dark_red` execution > 5 × estimated
planner capacity (default: 10 actions/planner/day).
Impact: Planners cannot execute all signals; lowest-priority items will be
ignored, defeating the buffer system.
Action: Emit a `portfolio` signal listing the top-priority items (by
inventory-value × severity) that fit the team's capacity, and recommending
the rest be deferred or auto-acted-on. Severity: `high`.

### Rule RP-2 — Receiving Capacity Saturation
Condition: Total inbound POs scheduled within next 7 days × average units
per PO exceeds known receiving throughput.
Impact: Buffer-replenishment delays due to dock congestion → cascading red
states.
Action: Emit a `stockout_risk` signal proposing PO release staggering.
Severity: `medium`.

### Rule RP-3 — Skill-Mix Gap
Condition: Item requires specialized handling (e.g., hazmat, temperature
control, customs) but the planner roster lacks that skill flag.
Impact: PO release blocked; buffer cannot replenish.
Action: Emit a `supplier_risk` signal flagging the skill gap. Severity:
`high` for A-items, `medium` otherwise.

### Rule RP-4 — Productivity Discount Ignored
Condition: An offshore/3PL supplier is treated as 1.0 productivity but
historical on-time rate < 0.8.
Impact: Effective lead time is longer than DLT; buffer red zone is
under-protected.
Action: Emit a `buffer_resizing` signal proposing LTF/VF increase to reflect
the productivity discount. Severity: `medium`.

### Rule RP-5 — Budget-Constrained Expediting
Condition: Sum of recommended expediting costs > stated expediting budget
ceiling.
Impact: Recommendations are infeasible against the budget.
Action: Apply PPO-style ranking (return ÷ cost) and emit a `portfolio`
signal listing the affordable subset. Severity: `medium`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "high",
  "part_number": "WORKFORCE",
  "title": "Planner overload — 38 red items vs capacity for 20",
  "detail": "38 items are red/dark_red. Planner team = 2 (capacity ~20 actions/day). Top-20 by (inventory_value × severity_weight) will recover this week; bottom-18 are deferred. Items deferred include 6 A-class items — flag risk to leadership.",
  "recommendation": "(1) Run today: top 20 items (list attached). (2) Escalate: 6 A-class items deferred → request planner overtime or auto-PO for these. (3) Re-evaluate buffer profiles for chronic-red C items to reduce future planner load.",
  "metric_name": "planner_overload_factor",
  "metric_value": 1.9,
  "metric_threshold": 1.0
}
```

### What NOT to output
- Do not propose hiring/staffing without explicit capacity data.
- Do not emit RP signals if the snapshot has < 10 items in red.

Max signals per run: 5.
