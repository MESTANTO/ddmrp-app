# Skill 05 — Safety Stock Optimization

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `SAFETY STOCK GAPS`.

This skill governs how to evaluate whether the red zone (DDMRP safety protection) is
correctly sized, identify items where safety stock is too low or too high, and
recommend parameter corrections with quantified financial and service impact.

In DDMRP, the red zone IS the safety stock. There is no separate "safety stock" field.
The red zone is calculated as:

  Red zone = Red zone base + Red zone safety
  Red zone base = ADU × DLT × LTF
  Red zone safety = Red zone base × VF

The agent must work with this definition, not with generic safety stock formulas
that are not applicable to DDMRP-managed items.

---

## Data Source in This App

| Field | Table | Use |
|---|---|---|
| `top_of_red` | buffers | Current red zone (= safety stock) |
| `adu` | items | Average daily usage |
| `dlt` | items | Decoupled lead time (days) |
| `ltf` | items | Lead time factor (default 0.5) |
| `vf` | items | Variability factor (default 0.5) |
| `on_hand` | items | Current physical stock |
| `unit_cost` | items | For financial impact calculation |
| `execution_color` | buffers | Current execution status |
| `buffer_status_pct` | buffers | On-hand as % of TOR |

Pre-computed values available in the agent context under `SAFETY STOCK GAPS`:
- `on_hand`: current physical stock
- `top_of_red`: current red zone threshold
- `gap_units`: `top_of_red - on_hand` (how far below TOR the item is)
- `gap_days_of_cover`: `gap_units / adu` (days of demand the gap represents)

---

## DDMRP Red Zone Sizing Logic

### Minimum acceptable red zone

For any DDMRP-managed item, the absolute minimum red zone must cover:
- At least 1 full replenishment cycle under worst-case demand
- Formula: minimum TOR = ADU × DLT × (LTF + VF)

If `top_of_red < ADU × DLT × 0.3`: the red zone is critically undersized.
This item has almost no safety protection.

### Maximum acceptable red zone

A red zone that is too large locks capital unnecessarily.
Maximum reasonable TOR = ADU × DLT × 1.5

If `top_of_red > ADU × DLT × 1.5`: the red zone is likely oversized.
Review if VF and LTF are set too conservatively.

---

## Analysis Rules

### Rule SS-1 — On-Hand Below Red Zone (Active Gap)

Condition: `items.on_hand < buffers.top_of_red`

The item is inside the red zone. Physical stock has dropped below the safety
protection level. This is the primary execution alarm.

Gap value = `(top_of_red - on_hand) × unit_cost`
Gap days = `gap_units / adu` (if adu > 0)

This rule overlaps with BUF-2 (buffer skill). When both skills apply:
- Use BUF-2 for the main operational signal
- Use this skill (SS-1) to add the safety stock adequacy context

Severity: same as BUF-2 — `critical` for A items or dark_red, `high` otherwise

### Rule SS-2 — Red Zone Too Small vs Calculated Minimum

Condition: `buffers.top_of_red < items.adu × items.dlt × (items.ltf + items.vf) × 0.8`

The current red zone is more than 20% below its own theoretical formula result.
This means either:
1. ADU or DLT used for the buffer calculation was lower than current values
2. LTF or VF was manually overridden to a very low value
3. Buffer was never recalculated after parameter updates

Calculated minimum TOR = `adu × dlt × (ltf + vf)`
Current TOR = `top_of_red`
Gap: `(calculated_min - current_tor)` units and `× unit_cost` value

Severity:
- A-class: `high`
- B-class: `medium`
- C-class: `low` unless execution color is red/dark_red

signal_type: `safety_stock_gap`

### Rule SS-3 — Red Zone Too Large (Capital Over-Protection)

Condition: `buffers.top_of_red > items.adu × items.dlt × 1.5`
AND `items.on_hand > buffers.top_of_red × 2` (item is not actually at risk)

The safety zone is oversized and capital is being locked as excess protection.
This is typical when:
1. VF was set very high (> 0.8) without justification
2. LTF is set to 1.0 for a stable item
3. The buffer was configured for a past peak period

Excess protection value = `(current_tor - recommended_tor) × unit_cost`
Recommended TOR = `adu × dlt × (ltf + min(vf, 0.5))`

Severity: `medium` for A-class (cash impact), `low` for others

signal_type: `safety_stock_gap`

### Rule SS-4 — Zero Safety Protection on Active Item

Condition: `buffers.top_of_red = 0` OR `buffers.top_of_red IS NULL`
AND `items.adu > 0` AND `items.dlt > 0`

The item has demand and lead time but zero safety protection. Any demand
variability or supply delay will result in a stockout. This is a critical
configuration gap.

Expected minimum TOR = `adu × dlt × 0.5` (conservative estimate)
Expected minimum TOR value = `expected_tor × unit_cost`

Severity: `critical` for A/B items, `high` for C items

### Rule SS-5 — Service Level vs Safety Stock Adequacy

For each item where on_hand is below TOR, calculate the implied days of supply
at current ADU:

  days_of_supply = on_hand / adu

If days_of_supply < dlt: the item will stock out before the next replenishment
can arrive, even if an order is placed today.

  stockout_risk_value = (dlt - days_of_supply) × adu × unit_cost
  (this is the value of unserviceable demand if replenishment is not expedited)

Always include this calculation when on_hand < top_of_red AND ADU > 0.

---

## Output Format

signal_type: `safety_stock_gap`

Examples:

```json
{
  "signal_type": "safety_stock_gap",
  "severity": "high",
  "part_number": "ITEM-022",
  "title": "Red zone too small — 35% below calculated minimum [ITEM-022]",
  "detail": "Current top_of_red = 60 units. Calculated minimum (ADU × DLT × (LTF + VF) = 12.0 × 14 × (0.5 + 0.45)) = 159.6 units. The buffer was likely calculated with an older, lower ADU. Current gap: 99.6 units (€1,990 at €20/unit). With current on_hand = 145 units, the item is in the yellow zone but has less protection than its parameters imply. If demand spikes, the red zone will be penetrated with insufficient warning.",
  "recommendation": "Recalculate buffer for ITEM-022 using current ADU = 12.0. New top_of_red = ~160 units. Update in Material Master > Buffer parameters. Verify LTF (currently 0.5) and VF (currently 0.45) are appropriate for this item's variability profile.",
  "metric_name": "tor_gap_vs_calculated_pct",
  "metric_value": -35,
  "metric_threshold": -20
}
```

```json
{
  "signal_type": "safety_stock_gap",
  "severity": "medium",
  "part_number": "ITEM-051",
  "title": "Red zone oversized — VF = 0.90 locks €4,200 excess protection [ITEM-051]",
  "detail": "Current top_of_red = 280 units = ADU × DLT × (LTF + VF) = 7.0 × 20 × (0.5 + 0.90). VF = 0.90 is appropriate only for highly volatile Z-class demand. This item has CV = 0.38 (X-class, stable demand). Recommended VF for X-class: 0.20–0.30. Recommended top_of_red with VF = 0.25: 105 units. Excess protection: 175 units × €24/unit = €4,200 tied in over-sized safety.",
  "recommendation": "Reduce VF for ITEM-051 from 0.90 to 0.25 (appropriate for X-class with CV = 0.38). Recalculate buffer. New top_of_red ≈ 105 units. No immediate stock action needed — current on_hand is above new TOR. The excess stock will naturally be consumed without new orders.",
  "metric_name": "vf",
  "metric_value": 0.90,
  "metric_threshold": 0.30
}
```

### Title format

- Below TOR: `"On-hand below red zone — N days to stockout [PART]"`
- TOR too small: `"Red zone too small — N% below calculated minimum [PART]"`
- TOR too large: `"Red zone oversized — VF = X locks €Y excess protection [PART]"`
- Zero TOR: `"Zero red zone on active item — no safety protection [PART]"`

### Mandatory content in detail field

Always state:
- Current top_of_red value
- Calculated minimum/maximum TOR using the formula
- Gap in units and in value (if unit_cost > 0)
- Current ADU, DLT, LTF, VF values
- Days of supply at current on_hand vs DLT

### Signal limits from this skill

Maximum 10 safety_stock_gap signals per run.
Priority: items below TOR first (active gap), then structural sizing issues.
For items already flagged by Skill 02 (stockout_risk), do not duplicate — only add
a safety_stock_gap signal if there is a structural sizing issue to fix beyond the
immediate expedite action.
