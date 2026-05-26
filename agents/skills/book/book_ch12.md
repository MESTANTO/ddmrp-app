# Skill — Book Ch12: Resource-Constrained Project Scheduling (RCPSP)

## When to Apply This Skill

Apply when the agent must reason about **multi-task, multi-resource
initiatives** where buffer changes or supply-chain redesigns become
projects in themselves — e.g., rolling out a new decoupling-point, executing
a multi-week consolidation, or sequencing data-quality remediation across
many items.

Triggers:
- The user requests a "plan" or "rollout" rather than a one-off action.
- Several inter-dependent tasks must be sequenced under shared resources
  (planner-hours, IT capacity, supplier qualifications).
- Multi-project portfolio of buffer improvements competes for budget.

---

## Key Concepts from Chapter 12

**Resource-Constrained Project Scheduling Problem (RCPSP)**:

Given:
- A set of activities with durations p_i and precedence relationships.
- Renewable resources (machines, planners) with per-period capacity K_k.
- Non-renewable resources (budget, cash) with total cap.
- Activities consume r_ik units of resource k.

Find a schedule (start times s_i) such that:
- Precedence: s_j ≥ s_i + p_i for (i,j) ∈ E.
- Resource capacity: at any time t, Σ_{i active at t} r_ik ≤ K_k.
- Objective: minimize makespan (or maximize NPV, etc.).

Variants:
- **Single-mode** vs **multi-mode** (multiple ways to execute, time/cost
  trade-offs).
- **Renewable** vs **non-renewable** resources.
- **CPM** (Critical Path Method) ignores resources → upper-bound on
  makespan; RCPSP tightens it.

DDMRP relevance:
- A "buffer profile redesign rollout" across N items is an RCPSP: each item
  is an activity, planner-hours and IT cycles are renewable resources,
  budget is non-renewable.
- Identifying the **critical path** of remediation tells the agent which
  data-quality fixes block downstream improvements.

---

## Data Source in This App

| Field | Used for |
|---|---|
| List of pending data-quality fixes | activities |
| Planner team capacity | renewable resource |
| Budget for SKU rationalization | non-renewable resource |
| Item dependencies (BOM, supplier groupings) | precedence |

---

## Analysis Rules

### Rule RCPSP-1 — Buffer-Redesign Rollout Without Plan
Condition: The agent has identified ≥ 10 items needing buffer-profile
changes but no rollout sequence is specified.
Impact: Simultaneous changes overload planner capacity; some changes will
be poorly executed or rolled back.
Action: Emit a `portfolio` signal proposing an RCPSP-style rollout in waves
of 3–5 items per week, ordered by inventory-value impact and dependency.
Severity: `medium`.

### Rule RCPSP-2 — Critical-Path Data-Quality Fix
Condition: Multiple data-quality issues exist, and at least one (e.g.,
missing unit_cost) blocks downstream financial analyses for many items.
Impact: This fix is on the critical path; other improvements are
sequentially blocked.
Action: Emit a `data_quality` signal labeling the fix as "critical path"
and recommending it be done first. Severity: `high`.

### Rule RCPSP-3 — Budget Exhaustion (Non-Renewable)
Condition: Proposed remediation actions (cost of new suppliers, expediting,
software changes) exceed stated annual budget.
Impact: Non-renewable resource exhausted mid-rollout.
Action: Emit a `portfolio` signal proposing a budget-feasible subset by
maximizing NPV (or expected inventory-cost reduction per $). Severity:
`high`.

### Rule RCPSP-4 — Precedence Violation in Action Plan
Condition: A proposed action depends on another action that hasn't been
scheduled yet (e.g., "switch to supplier B" before "qualify supplier B").
Impact: Logical infeasibility.
Action: Emit a `data_quality` signal listing the missing prerequisite and
inserting it before the dependent action. Severity: `medium`.

### Rule RCPSP-5 — Multi-Mode Execution Trade-Off
Condition: A remediation task can be done fast/expensive (consultant) or
slow/cheap (internal), and the choice is not specified.
Impact: Trade-off not surfaced; sub-optimal mode chosen by default.
Action: Emit a `portfolio` signal stating both modes with cost/duration.
Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "ROLLOUT",
  "title": "Buffer-redesign rollout — 18 items, 6-week RCPSP plan",
  "detail": "18 items need buffer-profile changes. Planner capacity = 6 buffer-changes/week. Critical-path activity: fix unit_cost on 3 items first (blocks financial review of 14 dependent items). Proposed RCPSP plan: Week 1 = 3 cost fixes (predecessor). Weeks 2-4 = 12 A-class buffer resizes (3 per planner). Weeks 5-6 = 3 C-class. Estimated makespan = 6 weeks. Budget consumed: $4,200 / $10,000.",
  "recommendation": "Approve the 6-week wave plan. Assign one planner as critical-path owner for week 1.",
  "metric_name": "rollout_makespan_weeks",
  "metric_value": 6,
  "metric_threshold": 8
}
```

### What NOT to output
- Do not produce RCPSP signals for single-item actions.
- Do not invent activities or resources not visible in the snapshot.

Max signals per run: 4.
