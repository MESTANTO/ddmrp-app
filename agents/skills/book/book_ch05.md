# Skill — Book Ch5: QUBO & Combinatorial Inventory Choices

## When to Apply This Skill

Apply when an inventory decision reduces to a **set of yes/no choices with
pairwise interactions** — e.g., which SKUs to include in a promotional bundle,
which warehouse slots to assign to which fast-movers, which suppliers form a
compatible roster. These are the kinds of problems where a Quadratic
Unconstrained Binary Optimization (QUBO) reformulation is natural.

In a DDMRP context, treat this as the skill for **discrete portfolio choices**
where item-pairs interact (cannibalization, slot adjacency, supplier overlap).

---

## Key Concepts from Chapter 5

A QUBO model:

```
min  xᵀ Q x       (objective is quadratic)
s.t. x ∈ {0,1}ⁿ   (binary; no other constraints)
```

The Q matrix encodes both:
- **Diagonal** terms `q_ii` = standalone cost/benefit of picking item i.
- **Off-diagonal** terms `q_ij` = pairwise interaction (penalty or reward
  when both i and j are selected).

Constraints can be embedded into the objective via **penalty functions** with
a large multiplier P:

- `x + y ≤ 1` → penalty `P · xy` (prevents both 1)
- `x + y ≥ 1` → penalty `P · (1 − x − y + xy)` (forces at least one)
- `x = y`     → penalty `P · (x + y − 2xy)`

Inventory-relevant problems that naturally fit QUBO:
- **Number Partitioning** — split SKUs into two warehouses with balanced
  workload.
- **Maximum Cut** — split a supplier portfolio into two non-overlapping pools.
- **Knapsack / Set Cover / Set Packing** — promotional-bundle selection,
  cross-docking slot assignment.

QUBO is amenable to metaheuristics (simulated annealing, tabu search) and to
quantum/annealing hardware. Practical takeaway for the agent: when a decision
has ~10–30 binary choices with strong pairwise interactions, frame it as QUBO
and call out the interaction matrix rather than enumerating.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.part_number` | binary decision: include/exclude this item |
| `items.unit_cost × adu` | diagonal q_ii (value of inclusion) |
| Item-pair correlation (computed) | off-diagonal q_ij (cannibalization, complementarity) |
| `items.default_supplier_id` | grouping for set-partition decisions |

---

## Analysis Rules

### Rule QUBO-1 — Cannibalization Pair Suggested
Condition: Two items share a supplier AND have highly correlated demand
(implicitly: substitutes). Both are buffered.
Impact: Holding buffers on both ties up double the cash for one consumer
demand stream.
Action: Emit a `portfolio` signal proposing a binary choice: buffer one
(the higher-margin or higher-volume) and treat the other as
made-/ordered-to-order. Severity: `medium`.

### Rule QUBO-2 — Bundle Selection Conflict
Condition: A proposed promotional/kit bundle contains items with conflicting
buffer states (one red, one overstocked).
Impact: Bundle execution will deplete already-red item further.
Action: Emit a `portfolio` signal recommending an alternative bundle
composition that uses overstocked items only. Severity: `medium`.

### Rule QUBO-3 — Supplier Portfolio Partition
Condition: > 6 active suppliers exist, with overlapping coverage and no
dual-source pairing.
Impact: Audit/onboarding overhead is high; supplier consolidation likely.
Action: Emit a `supplier_risk` signal recommending a partition into a
primary pool (high reliability, A-items) and a secondary pool (B/C items).
Severity: `low`.

### Rule QUBO-4 — Penalty-Constraint Modeling Hygiene
Condition: The agent is reasoning about a combinatorial choice with multiple
"must include / cannot include together" rules.
Impact: Without explicit penalty formulation, recommendations are
inconsistent across runs.
Action: Internally express the rules as quadratic penalties before issuing a
recommendation; cite the rules in the signal `detail`. Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "ITEM-Q014",
  "title": "Cannibalization pair — buffer only one of [ITEM-Q014, ITEM-Q015]",
  "detail": "ITEM-Q014 and ITEM-Q015 share supplier SUPP-3 and have 0.91 demand correlation across last 12 months. Both are buffered (combined on-hand value $48K). Pairwise interaction term in QUBO portfolio is strongly negative: keeping both buffered is dominated. Q014 has higher margin (32% vs 24%).",
  "recommendation": "Keep ITEM-Q014 buffered; switch ITEM-Q015 to make-to-order. Re-evaluate after 90 days.",
  "metric_name": "demand_correlation",
  "metric_value": 0.91,
  "metric_threshold": 0.7
}
```

### What NOT to output
- Do not propose QUBO portfolio decisions without explicit pairwise data.
- Do not enumerate more than 20 binary choices in a single signal.

Max signals per run: 6.
