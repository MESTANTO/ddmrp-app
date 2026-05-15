# Skill 04 — Demand Variability and Trend Analysis

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `DEMAND TRENDS`.

This skill governs how to interpret demand history, identify significant demand pattern
changes, flag items where the ADU no longer reflects reality, and recommend corrections.

---

## Data Source in This App

Demand history comes from the `demand_entries` table, filtered by `company_id` via
the parent `items` table.

| Field | Table | Use |
|---|---|---|
| `item_id` | demand_entries | Join to items |
| `date` | demand_entries | Date of demand entry |
| `quantity` | demand_entries | Demand quantity on that date |
| `demand_type` | demand_entries | Type (sales order, forecast, etc.) |
| `adu` | items | Current ADU stored in item master |
| `dlt` | items | Decoupled lead time (days) |
| `vf` | items | Variability factor — should reflect CV |
| `unit_cost` | items | For financial impact of ADU errors |
| `top_of_yellow` | buffers | ADU × DLT feeds this zone |
| `top_of_green` | buffers | ADU feeds this zone |

The agent context provides pre-computed demand trend data per item:
- `weekly_totals`: list of weekly demand quantities over 90-day window
- `mean`: mean weekly demand
- `std`: standard deviation of weekly demand
- `cv`: coefficient of variation (std / mean)
- `trend_direction`: 'up' / 'down' / 'flat'
- `trend_slope`: estimated weekly change in demand (units/week)

---

## Core Calculations the Agent Must Apply

### Coefficient of Variation (CV)

CV = standard deviation of demand / mean demand

| CV Range | Demand Pattern | XYZ Class |
|---|---|---|
| CV ≤ 0.5 | Stable | X |
| 0.5 < CV ≤ 1.0 | Variable | Y |
| CV > 1.0 | Highly variable / intermittent | Z |

A high CV means the buffer's red zone must be proportionally larger. If CV is high
but VF is low, the item is structurally under-protected.

### ADU Accuracy Check

Compare the item's current stored ADU against the ADU computed from recent demand:

`recent_adu = sum(demand last 60 days) / 60`

ADU divergence % = `abs(recent_adu - stored_adu) / stored_adu × 100`

If divergence > 25%: the buffer zones are calibrated on stale demand. Flag as
`demand_trend` signal.

### Trend Direction

A rising demand trend means:
- The buffer will be consumed faster than planned
- ADU should be increased
- Buffer zones will need upward recalibration

A declining demand trend means:
- Overstock risk is increasing
- ADU should be decreased
- Buffer zones will need downward recalibration

Trend slope significance threshold: ±10% of current ADU per week.
Below this threshold, classify as 'flat'.

---

## Analysis Rules

### Rule DEM-1 — ADU Significantly Outdated (Rising)

Condition: `recent_adu > stored_adu × 1.25` AND `trend_direction = 'up'`

The item's demand has grown materially. The buffer zones are undersized relative
to actual consumption. Stock will be consumed faster than the buffer expects.

Risk: The yellow zone will be exhausted more quickly. The order signal triggers
later than it should relative to actual demand. Stockout risk increases.

Recommended ADU correction = `recent_adu` (from last 60-day window)
Buffer impact: `top_of_yellow` will increase by (new_adu - old_adu) × DLT
Estimated buffer value change: (zone increase in units) × unit_cost

Severity:
- A-class item AND divergence > 40%: `high`
- A-class item AND divergence 25–40%: `medium`
- B/C-class: `medium` if divergence > 40%, else `low`

### Rule DEM-2 — ADU Significantly Outdated (Declining)

Condition: `recent_adu < stored_adu × 0.75` AND `trend_direction = 'down'`

The item's demand has declined. The buffer zones are oversized. Capital is locked
in excess protection.

Risk: Orders will be placed too frequently. On-hand will build above TOG.
The green zone is generating more replenishment than demand requires.

Excess buffer value estimate: (stored_adu - recent_adu) × DLT × unit_cost
(this is the value locked in an oversized yellow zone alone)

Severity:
- A-class item with excess buffer value > €5,000: `high`
- A-class item with lower excess: `medium`
- B/C-class: `low`

### Rule DEM-3 — High Variability on Buffered Item (CV Misalignment)

Condition: `cv > 1.0` AND `items.vf < 0.5`

The demand is highly variable (Z-class behavior) but the variability factor is
set for a stable item. The red zone provides insufficient protection for actual
demand swings.

Estimated correct VF for Z-class: 0.55–0.75
Current VF: `items.vf`
Current TOR: `buffers.top_of_red`
Estimated needed TOR: TOR recalculated with VF = 0.65
TOR gap: needed_TOR - current_TOR (units and value)

Severity: `high` if A/B-class, `medium` if C-class

### Rule DEM-4 — Intermittent Demand (Potential Dead Stock Risk)

Condition: More than 60% of weekly periods in the 90-day window have zero demand
AND `items.on_hand > 0`

The item has highly intermittent demand. Holding a standard buffer may not be
justified. Risk of stock becoming slow-moving.

Days of coverage = `items.on_hand / items.adu` (if adu > 0)
If days_of_coverage > 180: flag as potential slow-mover.

Severity: `medium` for B items, `low` for C items. Suppress for A items unless
coverage > 365 days.

### Rule DEM-5 — Demand Spike Not Reflected in ADU

Condition: max weekly demand in context window > mean × 3.0

An order spike occurred. If the item has a spike threshold configured
(`items.spike_threshold > 0`), check if the spike exceeds it. If
`items.spike_threshold = 0`, all demand is included in the NFP calculation
including exceptional spikes.

Spike value: peak week demand × unit_cost
Recommendation: Review the spike threshold configuration. If the spike was
a one-off event, verify it is excluded from ADU calculation.

Severity: `medium`

---

## Output Format

signal_type: `demand_trend`

Examples:

```json
{
  "signal_type": "demand_trend",
  "severity": "high",
  "part_number": "ITEM-014",
  "title": "ADU outdated — demand up 38%, buffer undersized [ITEM-014]",
  "detail": "Stored ADU = 8.2 units/day. Computed ADU from last 60 days = 11.3 units/day (+38%). Demand trend is rising. The yellow zone (ADU × DLT = 8.2 × 18 = 148 units) is undersized for actual consumption. At current demand rate, the yellow zone covers only 13 days instead of 18. Stockout risk is elevated if the next replenishment order is placed using the stale ADU signal.",
  "recommendation": "Update ADU for ITEM-014 from 8.2 to 11.3 units/day using the ADU from Actual Demand tool in Material Master (lookback: 60 days). Recalculate buffer zones. New top_of_yellow estimate: ~203 units. Review if current open supply orders are sufficient for the updated demand rate.",
  "metric_name": "adu_divergence_pct",
  "metric_value": 38,
  "metric_threshold": 25
}
```

```json
{
  "signal_type": "demand_trend",
  "severity": "medium",
  "part_number": "ITEM-033",
  "title": "High demand variability — VF too low for Z-class behavior [ITEM-033]",
  "detail": "Item ITEM-033 shows CV = 1.42 over the last 90-day window (weekly demand std = 28.4, mean = 20.0). This is Z-class variability. Current variability factor = 0.20, which is configured for stable (X-class) demand. The red zone = 45 units provides only 2.25 days of protection at mean demand, with no adjustment for the high variability. The actual demand range is 0–85 units/week.",
  "recommendation": "Increase variability factor for ITEM-033 from 0.20 to 0.65. Recalculate buffer zones. Expected new top_of_red ≈ 105 units, providing adequate protection for Z-class demand. If the high variability is caused by irregular large orders, review the spike threshold setting.",
  "metric_name": "cv",
  "metric_value": 1.42,
  "metric_threshold": 1.0
}
```

### Title format

- ADU outdated rising: `"ADU outdated — demand up N%, buffer undersized [PART]"`
- ADU outdated declining: `"ADU outdated — demand down N%, overstock building [PART]"`
- High variability: `"High demand variability — VF too low for Z-class behavior [PART]"`
- Intermittent: `"Intermittent demand — N% zero periods, review coverage [PART]"`
- Demand spike: `"Demand spike detected — Nx mean, review spike threshold [PART]"`

### Mandatory content in detail field

Always state in detail:
- Stored ADU and computed ADU (from context window)
- Divergence % or CV value
- DLT and the affected zone sizes (in units)
- Financial impact estimate if unit_cost > 0
- Trend direction and slope

### Signal limits from this skill

Maximum 8 demand_trend signals per run.
Priority order: A items first, then by ADU divergence % descending.
Suppress demand_trend signals for items with fewer than 10 demand entries in the window
(insufficient history — flag instead as data_quality).
