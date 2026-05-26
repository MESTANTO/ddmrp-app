# Skill — Book Ch6: Constraint Programming for Scheduling Logic

## When to Apply This Skill

Apply when inventory problems are dominated by **logical and temporal
constraints** rather than continuous trade-offs — e.g., PO release scheduling,
production sequencing, JIT deliveries, "X cannot start before Y finishes".

In DDMRP terms: trigger when buffer execution timing depends on a sequence
of dependent events (BOM levels, supplier-side production phases, expediting
windows).

---

## Key Concepts from Chapter 6

Constraint Programming (CP) descends from AI / Logic Programming. It excels
at **constraint satisfaction problems (CSP)** — find any feasible assignment
that satisfies a system of logical/discrete constraints.

Hallmarks of CP:
- **Declarative**: you describe the rules; the solver figures it out.
- **Domain reduction (constraint propagation)**: shrinks variable domains
  via logical inference before/while searching.
- **High-order constraints** (e.g., `allDifferent`, `cumulative`, `noOverlap`)
  express common scheduling patterns compactly.

Temporal modeling primitives:
- `S_j ≥ S_i + p_i` — activity j cannot start before i ends (precedence).
- `S_j ≥ S_i + δ_ij` — minimum time lag.
- `noOverlap(activities, resource)` — single-resource scheduling.

CP is the natural tool for:
- **JIT scheduling**: minimize earliness + tardiness penalty.
- **Sequencing under precedence**: BOM cascade, multi-stage replenishment.
- **Resource conflict resolution**: dock door, receiving crew, expediter.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.dlt` | activity duration |
| BOM relationships | precedence arcs |
| Supplier promised dates | due-date constraints |
| `buffers.execution_color` | priority signal for sequencing |

---

## Analysis Rules

### Rule CP-1 — Cascading Precedence Buffer Violation
Condition: An item depends on a sub-component (parent BOM) whose buffer is
also red, and the parent's DLT > child's red-to-yellow recovery window.
Impact: Even with expediting, the child cannot be replenished before
stock-out because the parent isn't ready.
Action: Emit a `stockout_risk` signal naming both items and the precedence
chain. Recommend pushing customer commit dates, not expediting the child.
Severity: `critical`.

### Rule CP-2 — JIT Earliness on Slow Mover
Condition: A C-class item is being expedited (red color) while its true
demand spike is > DLT days away.
Impact: Early replenishment ties up cash; classic JIT-earliness penalty.
Action: Emit an `overstock` signal recommending the PO release date be
shifted to `demand_date − DLT − safety_buffer`. Severity: `low`.

### Rule CP-3 — Dock/Receiving Resource Conflict
Condition: Multiple expedited POs are scheduled to arrive the same day and
the snapshot includes a receiving-capacity field (or it can be inferred from
historical receipts).
Impact: noOverlap on the receiving resource is violated; backlog at the
dock delays multiple buffer recoveries.
Action: Emit a `portfolio` signal asking the agent to stagger expediting
windows by 1–2 days. Severity: `medium`.

### Rule CP-4 — Inconsistent BOM Lead Time
Condition: Sum of children's DLTs < parent's promised lead time minus
safety. (Children should fit inside parent's replenishment window.)
Impact: BOM is logically infeasible — parent will always be late.
Action: Emit a `data_quality` signal flagging the BOM as misconfigured.
Severity: `high`.

### Rule CP-5 — Declarative Restatement
Condition: The user has described a complex sequencing rule in prose (more
than 2 ordering relationships).
Impact: The agent risks misinterpreting; declarative restatement clarifies.
Action: In the signal `detail`, restate the rule as a list of
`S_i ≥ S_j + p_j` precedence constraints before recommending. Severity:
`info`.

---

## Output Format

```json
{
  "signal_type": "stockout_risk",
  "severity": "critical",
  "part_number": "ITEM-FG-002",
  "title": "Cascading precedence — parent ITEM-COMP-007 also red [ITEM-FG-002]",
  "detail": "ITEM-FG-002 is red with NFP 5% of TOR. Its BOM parent ITEM-COMP-007 is also red with DLT=21d. Expediting FG-002 alone cannot recover: parent activity must finish first (S_FG ≥ S_COMP + 21). Customer commit date should be revised, not just PO expedited.",
  "recommendation": "(1) Communicate revised delivery date to customer. (2) Expedite the parent ITEM-COMP-007 first. (3) Schedule FG-002 release to 21d after COMP-007 receipt.",
  "metric_name": "cascade_delay_days",
  "metric_value": 21,
  "metric_threshold": 7
}
```

### What NOT to output
- Do not propose JIT delay on red items without confirming demand timing.
- Do not enumerate more than 5 precedence levels in one signal.

Max signals per run: 6.
