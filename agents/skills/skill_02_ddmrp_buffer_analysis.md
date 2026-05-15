# Skill 02 — DDMRP Buffer Zone and Net Flow Position Analysis

## When to Apply This Skill

Apply this skill when the agent context contains sections labelled
`CRITICAL EXECUTION ALARMS`, `LOW NET FLOW POSITION ITEMS`, or `BUFFER SIZING ISSUES`.

This skill is the core DDMRP diagnostic. It governs how to read buffer zones, interpret
execution colors, evaluate net flow position, and identify buffers that need resizing.

---

## DDMRP Fundamentals This Agent Must Know

### The Three Buffer Zones

In this app, buffer zones are stored in the `buffers` table per item.

| Zone | Field | Meaning |
|---|---|---|
| Red zone | `top_of_red` | Safety protection. On-hand below this = stockout risk. |
| Yellow zone | Between `top_of_red` and `top_of_yellow` | Covers demand during decoupled lead time. |
| Green zone | Between `top_of_yellow` and `top_of_green` | Replenishment cycle. Drives order quantity. |

Formula reference (how zones were calculated):
- Red zone base = `items.adu × items.dlt × items.ltf`
- Red zone safety = Red zone base × `items.vf`
- `top_of_red` = Red zone base + Red zone safety
- `top_of_yellow` = `top_of_red` + (ADU × DLT)
- `top_of_green` = `top_of_yellow` + max(ADU × min_order_cycle, MOQ)

### Net Flow Position

Formula: `buffers.net_flow_position` = on_hand + on_order (open supply) − qualified demand

The app stores the pre-calculated NFP in `buffers.net_flow_position`.
It also stores the ratio in `buffers.buffer_status_pct` = (on_hand / top_of_red) × 100.

### Execution Colors

The app stores `buffers.execution_color` with these values and their meaning:

| Color | Condition | Meaning |
|---|---|---|
| `dark_red` | on_hand ≤ 0 | Stockout — no physical stock |
| `red` | on_hand < top_of_red | Below safety protection — critical risk |
| `yellow` | top_of_red ≤ on_hand < top_of_yellow | In demand-coverage zone — normal |
| `green` | on_hand ≥ top_of_yellow | In replenishment zone — healthy |

Note: execution color is based on on_hand only. NFP is the planning signal.

---

## Data Source in This App

Primary table: `buffers` filtered by `company_id` via join to `items`.
Supporting table: `items` for ADU, DLT, LTF, VF, unit_cost, item_type.

| Field | Table | Use in this skill |
|---|---|---|
| `net_flow_position` | buffers | Core planning signal |
| `buffer_status_pct` | buffers | On-hand as % of TOR (execution health) |
| `execution_color` | buffers | Immediate execution status |
| `top_of_red` | buffers | Safety threshold |
| `top_of_yellow` | buffers | Lead time demand threshold |
| `top_of_green` | buffers | Max replenishment target |
| `on_hand` | items | Physical stock |
| `adu` | items | Average daily usage |
| `dlt` | items | Decoupled lead time (days) |
| `unit_cost` | items | For financial impact calculation |

---

## Analysis Rules

### Rule BUF-1 — Stockout (Dark Red)

Condition: `buffers.execution_color = 'dark_red'` OR `items.on_hand <= 0`

This is the most critical signal. Physical stock is zero or negative. Demand is
being or will be missed immediately.

Actions from the recommended library:
- Expedite open supply immediately
- Check if a substitute material exists (`items` with same category)
- Pull stock from another site if multi-site data exists
- Escalate to procurement

Severity: `critical`

Metric: `metric_name = "on_hand"`, `metric_value = items.on_hand`, `metric_threshold = 0`

Financial impact to calculate and state in detail:
`shortage_value = abs(on_hand) × unit_cost` (if on_hand < 0)
Days until stockout: 0 (already stocked out)

### Rule BUF-2 — Red Zone Penetration (Stockout Risk)

Condition: `buffers.execution_color = 'red'`
i.e. `items.on_hand > 0` AND `items.on_hand < buffers.top_of_red`

The item is inside the safety protection zone. A stockout is imminent if no supply
arrives within the item's DLT.

Days until stockout estimate: `items.on_hand / items.adu` (if ADU > 0)
Shortage quantity: `buffers.top_of_red - items.on_hand`
Shortage value: shortage quantity × `items.unit_cost`

Actions:
- Expedite any open supply for this item
- If no open supply exists, place an emergency order: quantity = `top_of_green - net_flow_position`
- Review if DLT is realistic given supplier's current performance

Severity:
- A-class item OR days_until_stockout < DLT: `critical`
- B/C-class item AND days_until_stockout ≥ DLT: `high`

Metric: `metric_name = "buffer_status_pct"`, `metric_value = buffer_status_pct`, `metric_threshold = 100`

### Rule BUF-3 — Low Net Flow Position (Planning Risk)

Condition: `buffers.net_flow_position < buffers.top_of_yellow`

The planning signal to order has been triggered. The NFP has fallen below the
top of yellow, meaning demand during the replenishment cycle is not covered.

Recommended order quantity: `top_of_green - net_flow_position`
Adjust for MOQ: if `items.moq > recommended_qty`, flag MOQ-driven excess.

NFP ratio: `net_flow_position / top_of_red` — the lower this ratio, the more urgent.

Severity:
- NFP < top_of_red: `high` (planning signal AND execution risk)
- NFP < top_of_yellow × 0.5: `medium`
- NFP < top_of_yellow: `low`

Metric: `metric_name = "nfp_pct_of_tor"`, `metric_value = net_flow_position / top_of_red * 100`, `metric_threshold = 100`

### Rule BUF-4 — Buffer Too Small (Persistent Red Penetration Pattern)

Condition: `buffers.execution_color IN ('red', 'dark_red')` AND
`items.adu > 0` AND `buffers.top_of_red < items.adu × items.dlt × 0.5`

The red zone is smaller than 50% of one DLT worth of demand. The buffer is
structurally too small for this item's demand and lead time.

Root cause hypotheses:
1. ADU is outdated (too low) — demand has grown
2. DLT increased but buffer was not recalculated
3. Variability factor (VF) is too low for this item's demand pattern
4. Lead time factor (LTF) is too low

Recommended fix: Recalculate the buffer using current ADU from the last 30/60/90-day
demand window. In this app, use the "ADU from Actual Demand" feature in Material Master.

Severity: `high`
signal_type: `buffer_resizing`

Metric: `metric_name = "top_of_red"`, `metric_value = top_of_red`, `metric_threshold = adu × dlt × 0.5`

### Rule BUF-5 — Buffer Too Large (Persistent Green Excess)

Condition: `items.on_hand > buffers.top_of_green × 1.5`

Stock is persistently above the top of green. Capital is locked in excess buffer.

Excess quantity: `items.on_hand - buffers.top_of_green`
Excess value: excess quantity × `items.unit_cost`

Root cause hypotheses:
1. ADU is outdated (too high) — demand has declined
2. MOQ forces orders larger than the green zone
3. Lead time factor or variability factor is too high
4. Buffer was set up for a peak demand period that has passed

Actions:
- Reduce DDMRP buffer size by recalculating with current ADU
- Review MOQ with supplier if MOQ forces overage
- Freeze replenishment orders until stock drops below top of green

Severity:
- A-class: `high` (capital impact)
- B-class: `medium`
- C-class: `low`

signal_type: `buffer_resizing`
Metric: `metric_name = "on_hand_vs_tog_pct"`, `metric_value = on_hand / top_of_green * 100`, `metric_threshold = 150`

---

## Output Format

signal_type must be one of: `stockout_risk`, `low_nfp`, `buffer_resizing`

Examples:

```json
{
  "signal_type": "stockout_risk",
  "severity": "critical",
  "part_number": "ITEM-001",
  "title": "STOCKOUT — zero on-hand, demand at risk [ITEM-001]",
  "detail": "Item ITEM-001 has on_hand = 0 units. Execution color: dark_red. ADU = 12.5 units/day. No buffer protection remaining. Any open demand is unserviceable until supply arrives.",
  "recommendation": "Expedite the next supply order immediately. Recommended emergency order quantity: 450 units (top_of_green - NFP). Contact supplier to confirm earliest delivery. Check if ITEM-999 (same category) can substitute in the short term.",
  "metric_name": "on_hand",
  "metric_value": 0,
  "metric_threshold": 0
}
```

```json
{
  "signal_type": "buffer_resizing",
  "severity": "high",
  "part_number": "ITEM-042",
  "title": "Buffer too large — on-hand 210% of TOG, excess €8,400 [ITEM-042]",
  "detail": "On-hand = 840 units. Top of green = 400 units. Excess above TOG = 440 units × €19.09/unit = €8,400. ADU = 4.5 units/day, DLT = 14 days. Buffer appears sized on a higher ADU. Demand trend is flat-to-declining over last 90 days.",
  "recommendation": "Freeze replenishment for ITEM-042 until stock drops below TOG (400 units). Recalculate buffer ADU using last 60-day demand window. Expected buffer ADU correction: from ~6.5 to 4.5 units/day. Estimated cash release if no new orders placed: €8,400 over ~98 days.",
  "metric_name": "on_hand_vs_tog_pct",
  "metric_value": 210,
  "metric_threshold": 150
}
```

### Title format for buffer signals

- Stockout: `"STOCKOUT — zero on-hand, demand at risk [PART]"`
- Red zone: `"Red zone — stockout in ~N days, shortage €X [PART]"`
- Low NFP: `"Low NFP — order signal active, qty Y needed [PART]"`
- Buffer too small: `"Buffer undersized — red zone < 50% of DLT demand [PART]"`
- Buffer too large: `"Buffer oversized — on-hand N% of TOG, excess €X [PART]"`

### Mandatory content in detail field

Always include in the detail string:
- Current on_hand value and unit
- Execution color
- ADU and DLT
- top_of_red, top_of_yellow, top_of_green values
- Net flow position
- Days until stockout estimate (if in red)
- Financial impact (excess or shortage value) if unit_cost > 0

### Priority ordering

When generating buffer signals, sort by severity then by financial impact:
1. dark_red items first
2. red items by days_until_stockout ascending
3. low_nfp items by nfp_ratio ascending
4. buffer_resizing by excess/shortage value descending

Maximum buffer-related signals per run: 20.
