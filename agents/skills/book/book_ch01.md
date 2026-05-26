# Skill — Book Ch1: Optimization Modeling Fundamentals

## When to Apply This Skill

Apply this skill when the user asks to **structure an inventory decision as an
optimization problem**, or when the agent context contains a `snapshot` that
exhibits multiple competing objectives (e.g., service level vs. cash tied up,
supplier cost vs. lead time, stock-out risk vs. overstock).

Use this skill as the *lens* for framing any inventory question that requires
trading off conflicting goals. It does not produce buffer-sizing numbers — it
produces structured **decision frames** with explicit decision variables,
objective, and constraints.

---

## Key Concepts from Chapter 1

A formal optimization model has three components:

1. **Decision variables** — the choices to be made (e.g., order quantity per
   item, supplier allocation, reorder timing).
2. **Objective function** — a single (or weighted) quantitative goal:
   minimize cost, minimize stock-outs, maximize service level, etc.
3. **Constraints** — equalities/inequalities that bound feasibility (budget
   limits, supplier MOQ, warehouse capacity, contractual minimums).

Categories of mathematical programming models (Table 1.1 of the book):

| Class | Variable domain | Use in inventory |
|---|---|---|
| LP   | Continuous reals | Allocate order quantities across suppliers |
| ILP  | Integers         | Number of full pallets / containers to order |
| BIP  | Binary {0,1}     | Open/close supplier, include/exclude SKU in a kit |
| MILP | Real + integer   | Multi-period inventory planning with setups |
| NLP  | Nonlinear        | Holding costs that scale with on-hand squared |
| MINLP| Mixed nonlinear  | Supply chain configuration with economies of scale |

Three pillars of business analytics:

- **Descriptive** — what happened (current on-hand, last quarter consumption).
- **Predictive** — what will happen (ADU forecast, demand spike forecast).
- **Prescriptive** — what to do (buffer resize, expediting decision). This is
  where optimization sits, and where DDMRP buffer recommendations live.

Decision hierarchy (Figure 1.3):

- **Strategic** (>3 yrs) — network design, decoupling-point placement.
- **Tactical** (1–2 yrs) — buffer profile assignment, supplier selection,
  ABC-XYZ policy.
- **Operational** (daily/weekly) — execution color action, expediting, PO
  release.

---

## Analysis Rules

### Rule OPT1-1 — Unframed Multi-Objective Question
Condition: User question (or agent reasoning) mentions two or more competing
objectives without an explicit trade-off rule (e.g., "reduce inventory but keep
service ≥ 98%").
Impact: Recommendations are ambiguous and not reproducible.
Action: Emit a `portfolio` signal that explicitly states (a) the decision
variable(s), (b) the chosen objective, (c) the constraints. Severity: `medium`.

### Rule OPT1-2 — Wrong Decision Level
Condition: The user asks an operational question (e.g., "should I expedite
this PO?") but the symptom traces to a tactical/strategic root (wrong buffer
profile, bad supplier choice).
Impact: Operational fixes will repeat indefinitely.
Action: Emit a `portfolio` signal labeling the correct decision level (tactical
or strategic) and naming the item(s) involved. Severity: `medium`.

### Rule OPT1-3 — Prescription Without Prediction
Condition: The agent is about to recommend a buffer resize using ADU = 0 or
ADU based on < 30 days of history.
Impact: Prescriptive recommendation is built on absent predictive analytics.
Action: Refuse to issue the operational recommendation. Emit a `data_quality`
signal asking for more demand history first. Severity: `high`.

### Rule OPT1-4 — Missing Constraint Awareness
Condition: A proposed order quantity violates a known constraint (MOQ, max
order, budget envelope visible in the snapshot).
Impact: Recommendation is infeasible.
Action: Emit a `portfolio` signal listing the violated constraint and the
nearest feasible alternative. Severity: `high`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "ITEM-001",
  "title": "Decision frame missing — service-vs-cash trade-off unstated [ITEM-001]",
  "detail": "Two competing objectives detected (cash reduction and 98% service). Without an explicit weight or constraint, multiple incompatible recommendations are possible. Decision variable: order quantity. Constraints visible: MOQ=50, supplier lead time=14d. Objective should be declared.",
  "recommendation": "Choose ONE primary objective. Suggested: minimize on-hand value subject to projected stock-out probability ≤ 2%. Re-run analysis with explicit objective.",
  "metric_name": "objective_count",
  "metric_value": 2,
  "metric_threshold": 1
}
```

Title format: `[Frame name] — [missing element or violation] [PART_NUMBER]`

### What NOT to output
- Do not emit a signal if the user explicitly stated the objective and
  constraints already.
- Do not duplicate signals raised by other DDMRP skills; reference them.

Max signals per run: 10.
