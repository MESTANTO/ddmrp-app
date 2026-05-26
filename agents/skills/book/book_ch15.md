# Skill — Book Ch15: Credit Term & Supply Chain Finance

## When to Apply This Skill

Apply when inventory decisions intersect with **payment terms, working
capital, and cash flow** — i.e., when the *financial* dimension of buffer
sizing or PO sequencing is non-trivial.

DDMRP-relevant triggers:
- Snapshot includes large on-hand value tying up working capital.
- Supplier payment terms vary across the supplier base.
- A "cash release" or "DSO/DPO" question is implied.
- Quantity discounts vs. holding cost trade-offs are present.

---

## Key Concepts from Chapter 15

**Credit term** δ = days of delayed payment offered by a supplier to a buyer.
Longer δ:
- Frees buyer's working capital → may induce buyer to order more.
- Costs supplier interest on account receivable.

**Basic CTOP** (single-decision-maker, supplier perspective):

```
max  Σ_t [ p·(d_t0 + θ δ_t) − c x_t − h I_t − r δ_t p (d_t0 + θ δ_t) ]
s.t. I_{t-1} + x_t − (d_t0 + θ δ_t) = I_t
     x_t ≤ K
     δ_t, x_t, I_t ≥ 0
```

The last term in the objective (`r δ_t p d_t`) is **nonlinear** — interest
× credit-term × demand — and is the heart of the trade-off.

**Bilevel CTOP (BLCTOP)** — supplier leader, buyer follower (Stackelberg
game):
- Supplier sets δ first to maximize its profit.
- Buyer responds with order quantity given δ.
- Linking variables: δ (supplier → buyer) and order qty (buyer → supplier).

Key supply chain finance levers visible from this chapter:
1. **Quantity discount rate β** — encourages larger orders, ties up cash.
2. **Economies of scale α** — unit production cost drops with volume.
3. **Maximum end-of-horizon inventory cap Γ** — terminal condition.
4. **Interest rate r** — opportunity cost of cash tied up.

DDMRP relevance:
- Working capital tied up in green/yellow zones × r = silent interest
  expense the DDMRP buffer math doesn't show explicitly.
- A supplier offering longer payment terms in exchange for larger lot-size
  is a CTOP-style trade-off — sometimes worth accepting overstock to
  unlock cash.

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.unit_cost × on_hand` | working-capital tied up |
| `suppliers.payment_terms_days` | δ |
| `suppliers.quantity_discount` (if present) | β |
| `items.moq`, `items.adu`, `items.dlt` | order frequency |
| Interest rate (config or default 8%/yr ≈ 0.022%/day) | r |

---

## Analysis Rules

### Rule FIN-1 — Hidden Interest Cost on Overstock
Condition: An item has on-hand ≥ 1.5 × top_of_green AND unit_cost × excess >
$10,000.
Impact: At standard cost of capital (8–12%/yr), this excess silently bleeds
hundreds-to-thousands of dollars per year in interest.
Action: Emit an `overstock` signal that includes the dollar interest cost
in the recommendation. Severity: `medium` (escalate to `high` if value >
$50K).

### Rule FIN-2 — Payment Terms Misalignment
Condition: A supplier offers Net-15 (short credit) and is paid before
inventory is consumed, while a near-substitute supplier offers Net-60.
Impact: Cash-conversion-cycle longer than necessary.
Action: Emit a `supplier_risk` signal proposing a re-allocation toward the
longer-payment supplier (subject to reliability). Severity: `low`.

### Rule FIN-3 — Quantity Discount vs Holding Trade-Off
Condition: A supplier offers a quantity discount but the implied order
quantity exceeds top_of_green × 2.
Impact: Discount savings may not exceed the interest + holding cost.
Action: Compute the net: discount savings vs. (additional holding × cost
of capital). Emit a `portfolio` signal stating the winner. Severity:
`medium`.

### Rule FIN-4 — Bilevel Game Awareness
Condition: A supplier is renegotiating credit terms tied to volume
commitments.
Impact: The supplier is the Stackelberg leader; ignoring the demand-on-δ
elasticity θ may lead to suboptimal acceptance.
Action: Emit a `portfolio` signal recommending that the negotiation be
modeled as supplier-leads with explicit θ estimation. Severity: `low`.

### Rule FIN-5 — End-of-Period Inventory Cap Risk
Condition: Projected inventory at end of fiscal/reporting period for
high-value items will exceed historical levels significantly.
Impact: Working capital ratios degrade; CFO-visible KPI deteriorates.
Action: Emit an `overstock` signal recommending PO pull-in deferral or
returns to supplier. Severity: `medium`.

---

## Output Format

```json
{
  "signal_type": "overstock",
  "severity": "high",
  "part_number": "ITEM-F099",
  "title": "Hidden interest cost — overstock bleeds $4,800/yr [ITEM-F099]",
  "detail": "ITEM-F099 has on-hand 18,400 units vs top_of_green 6,800. Excess = 11,600 × unit_cost $5.20 = $60,320 tied up. At 8% cost of capital, this silently costs $4,826/yr in interest, plus est. $1,200 holding. Total carrying cost of excess: $6,026/yr. CTOP framing: if supplier offered Net-60 vs Net-15 in exchange for this level, the deal could be neutral; otherwise, drain inventory.",
  "recommendation": "(1) Hold POs until on-hand drops to top_of_green. (2) Negotiate Net-60 with supplier or push 30% of excess back if returnable. (3) Reassess after 60 days.",
  "metric_name": "annual_interest_cost_usd",
  "metric_value": 4826,
  "metric_threshold": 0
}
```

### What NOT to output
- Do not emit FIN signals when unit_cost is missing or zero.
- Do not propose payment-term changes for less-than-3-month-old supplier
  relationships.

Max signals per run: 6.
