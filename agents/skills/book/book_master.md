# Skill — Book Master: Optimization Modeling for Supply Chain (Haitao Li)

## When to Apply This Skill

Apply this skill as the **overarching mental framework** for any inventory
question that requires reasoning beyond a single DDMRP buffer rule. Use it
when:

- The user asks a broad strategic/tactical question ("what should we change
  in our network?", "where should we hold safety stock?").
- Multiple chapter-skills are relevant simultaneously and need orchestration.
- A trade-off between cost, service, and cash is at the center of the
  question.

This master skill **does not duplicate** the chapter skills; it points to
the right chapter for each decision class and provides the bridging framework
between DDMRP and Operations Research.

---

## The Three Pillars (Ch1)

Every DDMRP analysis the agent runs lives in one of three layers:

| Pillar | Looks at | DDMRP equivalent | Chapter |
|---|---|---|---|
| Descriptive  | What happened (history)      | ADU calculation, historical buffer color | base skills 1–4 |
| Predictive   | What will happen             | Demand forecast, spike detection         | base skill 9 |
| Prescriptive | What to do                    | Buffer resize, expediting, PO release    | base skills 5–8 + all book skills |

If a recommendation skips a pillar (e.g., prescribing without predicting),
issue a `data_quality` signal first (see Rule OPT1-3 in Ch1 skill).

---

## Decision-Hierarchy Map (Ch1)

| Level        | Horizon       | DDMRP touchpoint                | Book chapters |
|--------------|---------------|---------------------------------|---------------|
| Strategic    | > 3 years     | Decoupling point placement, network footprint | Ch7, Ch10 |
| Tactical     | 6 mo – 2 yr   | Buffer profile assignment, supplier selection, ABC-XYZ | Ch2, Ch8, Ch9, Ch10 |
| Operational  | days – weeks  | Execution color action, expediting, PO release, lateral transfers | Ch3, Ch4, Ch11, Ch13, Ch14 |
| Financial    | continuous    | Working capital, payment terms | Ch15 |

When the user asks an operational question whose **root cause** is tactical
or strategic, the agent should explicitly flag this (see Rule OPT1-2 in Ch1).

---

## Model-Class Routing Cheat Sheet

| Problem signature | Model class | Skill to call |
|---|---|---|
| Continuous allocation, linear trade-offs | LP | `book_ch02` |
| Discrete units / batch sizes / on-off decisions | ILP / BIP | `book_ch03` |
| Pairwise interaction binaries | QUBO | `book_ch05` |
| Sequencing / scheduling / temporal logic | CP | `book_ch06`, `book_ch11` |
| Flows over networks (multi-echelon, transshipment) | Network LP | `book_ch04` |
| Strategic location | MILP (UFLP/CFLP) | `book_ch07` |
| Multi-period production with setups | MILP (MPS/MRP) | `book_ch08` |
| Workforce / resource capacity | MILP (RP) | `book_ch09` |
| Safety stock placement + decoupling | MINLP / SSPP | `book_ch10` |
| Multi-task projects with dependencies | RCPSP | `book_ch12` |
| Single-vehicle multi-stop routing | TSP variants | `book_ch13` |
| Multi-vehicle routing / VMI / IRP | VRP variants | `book_ch14` |
| Credit terms / cash-flow trade-offs | NLP / bilevel | `book_ch15` |

---

## Master Analysis Rules

### Rule MASTER-1 — Orchestration Discipline
Condition: A single user query touches more than 3 chapter skills (e.g.,
"why are 30 items red and how should we re-design the network?").
Impact: Mixing strategic and operational recommendations produces unclear
priorities.
Action: Emit a `portfolio` signal that **decomposes** the question by
decision level (strategic / tactical / operational) and points to the
specific chapter skills to run, in order. Severity: `medium`.

### Rule MASTER-2 — Decoupling Point Is the Pivot
Condition: A network of multi-echelon items is being analyzed.
Impact: Decoupling point location is the single largest lever in total
inventory cost; all buffer-level decisions are subordinate to it.
Action: Always run `book_ch10` *before* `book_ch08` or per-item resizes
when buffers and BOM are involved. Severity: `info`.

### Rule MASTER-3 — Cost-of-Capital Always Visible
Condition: An overstock signal is being issued.
Impact: Without dollar-cost-of-capital, the recommendation feels theoretical.
Action: Always include the interest cost computed via `book_ch15` rules
(annual_interest_cost = excess_value × cost_of_capital).
Severity: `info`.

### Rule MASTER-4 — Don't Optimize Around Bad Data
Condition: Data-quality signals are open AND optimization-style
recommendations are being prepared.
Impact: Optimal solution to wrong inputs.
Action: Resolve `data_quality` signals first. Defer book-skill outputs that
depend on the affected fields. Severity: `high`.

### Rule MASTER-5 — Frame Every Decision
Condition: A book-skill recommendation is being emitted.
Impact: Without explicit decision-variable / objective / constraints
framing, the recommendation isn't reproducible.
Action: Each signal should include in its `detail`:
- the decision variable(s) implied
- the objective being optimized
- the binding constraint(s).
Use `book_ch01` framing language. Severity: `info`.

---

## Cross-Chapter Themes

**Theme A — Variability is the enemy.**
- Ch10 (decoupling): place stock so variability is absorbed cheaply.
- Ch4 (network): pool variability upstream when downstream variants share
  components.
- Ch15 (finance): variability that ties up cash has interest cost.

**Theme B — Capacity is finite.**
- Ch8 (MPS): production capacity.
- Ch9 (RP): planner / workforce capacity.
- Ch11 (scheduling): single-machine bottleneck.
- Ch14 (VRP): vehicle / route capacity.
- All four chapters share the same shadow-price intuition (Ch2): when a
  capacity binds, releasing it has outsized value.

**Theme C — Discrete choices need explicit framing.**
- Ch3 (IP), Ch5 (QUBO), Ch6 (CP), Ch7 (UFLP/CFLP), Ch12 (RCPSP), Ch13/Ch14
  (routing) — all deal with combinatorial structure. The agent should
  never enumerate; it should name the structure and propose a heuristic
  or solver framing.

**Theme D — Financial and operational decisions are coupled.**
- Ch15 (credit terms) shows demand depends on terms; terms depend on cost
  of capital; cost of capital scales with inventory.
- A pure operational fix that ignores cash is incomplete.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "MASTER",
  "title": "Multi-level decomposition — 3 chapter skills recommended in order",
  "detail": "User question spans network design (strategic), buffer profile (tactical), and red-queue execution (operational). Recommended order: (1) Ch10 — confirm decoupling-point placement (book_ch10). (2) Ch8 — re-evaluate MPS lot-sizes for the affected M-items (book_ch08). (3) Ch11 — sequence the resulting red queue by EDD (book_ch11). Cost-of-capital lens via Ch15 should be applied to any overstock recommendations.",
  "recommendation": "Run book_ch10 first. Do not proceed to book_ch08 or book_ch11 until decoupling-point conclusion is fixed. Reconcile data-quality issues before any of these.",
  "metric_name": "chapter_skills_invoked",
  "metric_value": 3,
  "metric_threshold": 1
}
```

### What NOT to output
- Do not duplicate signals already emitted by individual chapter skills.
- Do not invoke the master skill for narrow single-item operational
  questions — those are covered by the base DDMRP skills.

Max signals per run: 3.
