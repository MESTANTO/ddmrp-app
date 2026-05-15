# Skill 06 — Overstock and Excess Inventory Analysis

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `OVERSTOCK ITEMS`.

This skill governs how to identify excess inventory, quantify the financial exposure,
diagnose root causes, and recommend specific actions from the approved action library.

The primary objective is to release cash locked in excess stock WITHOUT creating
service risk. The agent must never recommend reducing stock on a critical or at-risk
item purely to reduce inventory value.

---

## Data Source in This App

| Field | Table | Use |
|---|---|---|
| `on_hand` | items | Current physical stock |
| `unit_cost` | items | For excess value calculation |
| `adu` | items | For days of coverage calculation |
| `dlt` | items | For coverage vs lead time comparison |
| `moq` | items | Minimum order quantity — may force excess |
| `order_multiple` | items | Lot sizing — may force excess |
| `top_of_green` | buffers | Maximum healthy stock level |
| `top_of_yellow` | buffers | Lead time demand coverage level |
| `top_of_red` | buffers | Safety protection threshold |
| `net_flow_position` | buffers | Planning signal (on_hand + supply − demand) |
| `execution_color` | buffers | green = in replenishment zone or above |

Pre-computed values available in context under `OVERSTOCK ITEMS`:
- `on_hand`: physical stock
- `top_of_green`: TOG from buffer
- `excess_units`: on_hand - top_of_green
- `excess_value`: excess_units × unit_cost (if unit_cost > 0)
- ABC class from classification

---

## Overstock Detection Rules

### Rule OS-1 — Stock Above Top of Green

Condition: `items.on_hand > buffers.top_of_green`

In DDMRP, TOG is the maximum healthy stock level. Stock above TOG means
the buffer is saturated. No new replenishment orders should be generated.

Excess quantity = `on_hand - top_of_green`
Excess value = `excess_quantity × unit_cost`
Days of excess = `excess_quantity / adu` (if adu > 0)

This is the primary overstock signal.

Severity:
- Excess value > €10,000 OR A-class: `high`
- Excess value €2,000–€10,000 OR B-class: `medium`
- Excess value < €2,000 AND C-class: `low`

### Rule OS-2 — Days of Coverage Extremely High

Condition: `items.adu > 0` AND `(items.on_hand / items.adu) > items.dlt × 3`

The item has more than 3× its decoupled lead time in stock. Even if TOG
comparison is not available, this level of coverage indicates overstock.

Days of coverage = `on_hand / adu`
Expected maximum coverage = `dlt × 2` (2 DLT cycles as a reasonable maximum)
Excess days = `days_of_coverage - (dlt × 2)`
Excess value estimate = `excess_days × adu × unit_cost`

Severity: same as OS-1 thresholds applied to excess value.

### Rule OS-3 — MOQ-Forced Excess

Condition: `items.on_hand > buffers.top_of_green`
AND `items.moq > (buffers.top_of_green - buffers.top_of_yellow)`

The green zone is smaller than the MOQ. Every time an order is placed, it
creates more stock than the buffer can absorb. The overstock is structurally
caused by the MOQ constraint, not by a demand or planning error.

MOQ excess estimate = `moq - (top_of_green - top_of_yellow)` units
MOQ excess value = `moq_excess × unit_cost`

Root cause: supplier minimum order quantity is too large for this item's demand.

Recommended action: Negotiate MOQ reduction with the supplier.
If MOQ cannot be reduced: increase green zone to accommodate MOQ.

Severity: `medium` regardless of value (structural issue, needs supplier discussion)

### Rule OS-4 — Overstock Despite Active Red/Yellow Signal

Condition: `items.on_hand > buffers.top_of_green`
AND `buffers.net_flow_position < buffers.top_of_yellow`

Paradox: physical stock is above TOG but NFP is below TOY. This happens when:
1. There is large qualified demand reducing NFP but stock hasn't been consumed yet
2. Open supply in transit is counted in NFP but hasn't been received

This item requires planner review — automated action is not appropriate.
Do not recommend cancelling orders until the demand/supply picture is clarified.

Severity: `medium` (flag for review, not for immediate action)

### Rule OS-5 — Slow-Moving Excess

Condition: `items.adu > 0` AND `(items.on_hand / items.adu) > 180`
AND no demand trend shows recovery (flat or declining trend)

The item has more than 6 months of stock and demand is not growing.
This stock is at risk of becoming obsolete if demand does not recover.

Slow-moving value = `on_hand × unit_cost`
Months of coverage = `(on_hand / adu) / 30`

Severity: `high` for A items, `medium` for B, `low` for C

---

## Root Cause Diagnosis

For every overstock signal, the agent must state one primary root cause from this list:

| Root Cause Code | Description | Indicator |
|---|---|---|
| ADU_HIGH | ADU was set higher than actual demand | recent_adu < stored_adu × 0.75 |
| MOQ_FORCED | MOQ > green zone width | items.moq > (tog - toy) |
| ORDER_EXCESS | Last order placed too large | on_hand >> tog with no demand drop |
| DEMAND_DROP | Demand decreased after order placed | trend_direction = 'down' |
| PHASE_OUT | Item in phase-out with no demand | adu ≈ 0 with stock |
| BUFFER_LARGE | Buffer TOG is too large for current demand | tog > adu × dlt × 3 |
| MANUAL_OVERRIDE | Order placed despite buffer signal | (requires planner input) |

---

## Action Library for Overstock

The agent must select from these specific actions — not generic advice:

| Action | When to Apply |
|---|---|
| Freeze replenishment orders | on_hand > TOG, execution color green or above |
| Cancel open purchase order | NFP very high (> TOG × 1.5), no demand in sight |
| Defer open purchase order | NFP high, some demand expected |
| Reduce order quantity | MOQ-driven excess, supplier willing to negotiate |
| Review MOQ with supplier | MOQ_FORCED root cause |
| Update ADU downward | ADU_HIGH root cause confirmed |
| Resize buffer TOG downward | BUFFER_LARGE root cause |
| Initiate obsolescence review | PHASE_OUT root cause or coverage > 365 days |
| Transfer to another site | Multi-site context (flag as requiring planner input) |

Always specify WHICH action applies and WHY, referencing the metric values.

---

## Financial Impact Calculation

For every overstock signal with unit_cost > 0, calculate and state:

1. **Excess quantity** = on_hand - top_of_green (units)
2. **Excess value** = excess_quantity × unit_cost (€)
3. **Holding cost** = excess_value × 0.20 / 365 × days_until_consumed
   (Use 20% annual holding cost as a default assumption)
4. **Opportunity** = excess_value (the cash that could be released)

State the holding cost in the detail field to create urgency around the cash release.

---

## Output Format

signal_type: `overstock`

Examples:

```json
{
  "signal_type": "overstock",
  "severity": "high",
  "part_number": "ITEM-003",
  "title": "Overstock €14,700 — on-hand 230% of TOG, freeze orders [ITEM-003]",
  "detail": "On-hand = 1,150 units. Top of green = 500 units. Excess above TOG = 650 units × €22.61/unit = €14,700. ADU = 8.5 units/day → 76 days to consume excess naturally. Root cause: ADU_HIGH — recent ADU (last 60 days) = 5.9 units/day vs stored ADU = 8.5 units/day. Buffer was calculated on inflated demand. Holding cost of excess at 20%/year ≈ €8.06/day. Total holding cost risk if no action: €616 over 76 days.",
  "recommendation": "1. Immediately freeze all open replenishment orders for ITEM-003. 2. Update ADU from 8.5 to 5.9 units/day using ADU from Actual Demand tool (60-day lookback). 3. Recalculate buffer zones — new TOG estimate ≈ 350 units. 4. Do not place new orders until on_hand drops below new TOG. Expected cash release timeline: 76 days at current consumption.",
  "metric_name": "on_hand_vs_tog_pct",
  "metric_value": 230,
  "metric_threshold": 100
}
```

```json
{
  "signal_type": "overstock",
  "severity": "medium",
  "part_number": "ITEM-018",
  "title": "MOQ forces excess — green zone < MOQ, structural overstock [ITEM-018]",
  "detail": "MOQ = 500 units. Green zone width (TOG - TOY) = 180 units. Every replenishment order creates 320 units more than the green zone can absorb. Current excess = 340 units × €8.50 = €2,890. Root cause: MOQ_FORCED. This will recur on every order cycle unless MOQ is renegotiated or the buffer TOG is increased to match the MOQ.",
  "recommendation": "Option A: Negotiate MOQ reduction with supplier from 500 to 200 units. Option B: Increase TOG to accommodate MOQ (new TOG = TOY + 500 = ~680 units). Option A is preferred as it reduces capital exposure. Include in next supplier review discussion.",
  "metric_name": "moq_vs_green_zone_width",
  "metric_value": 500,
  "metric_threshold": 180
}
```

### Title format

- Standard overstock: `"Overstock €X — on-hand N% of TOG, freeze orders [PART]"`
- MOQ-forced: `"MOQ forces excess — green zone < MOQ, structural overstock [PART]"`
- Slow-mover: `"Slow mover — N months of coverage, obsolescence risk [PART]"`
- Declining demand: `"Demand declining — stock will build, ADU correction needed [PART]"`

### Signal limits from this skill

Maximum 10 overstock signals per run.
Priority: by excess value descending (largest cash opportunity first).
Do not generate overstock signals for items in red or dark_red execution
(service risk takes priority — overstock action could cause stockout).
