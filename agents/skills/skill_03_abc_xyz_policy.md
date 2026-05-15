# Skill 03 — ABC/XYZ Segmentation and Policy Recommendations

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `ABC/XYZ CLASSIFICATION`.

This skill governs how to interpret the ABC/XYZ matrix, identify items with the wrong
planning policy for their segment, and recommend corrective actions at the portfolio level.

---

## Data Source in This App

The ABC/XYZ classification is pre-computed by `modules/classification.py` and passed
to the agent as structured data. The raw inputs used to compute it are:

| Field | Table | Used for |
|---|---|---|
| `part_number` | items | Item identifier |
| `unit_cost` | items | Annual consumption value (ABC) |
| `adu` | items | Estimated annual demand = adu × 365 |
| Annual value | computed | adu × 365 × unit_cost |
| CV (coeff. of variation) | computed from demand_entries | Demand variability (XYZ) |
| `abc_class` | computed | A / B / C |
| `xyz_class` | computed | X / Y / Z |

CV thresholds used in this app:
- X: CV ≤ 0.5 (stable)
- Y: 0.5 < CV ≤ 1.0 (variable)
- Z: CV > 1.0 (highly variable or intermittent)

ABC thresholds used in this app:
- A: cumulative annual value ≤ 70%
- B: cumulative annual value 70–90%
- C: cumulative annual value > 90%

---

## The 9-Cell Policy Matrix

For each ABC/XYZ combination, the agent must know the recommended planning policy
and be able to identify when the item's current setup deviates from it.

### AX — High value, stable demand

Recommended policy:
- Tight buffer sizing, high service target
- ADU must be accurate and updated frequently
- Safety stock (red zone) sized for low variability — VF should be low (0.1–0.3)
- Review buffer monthly
- Stockout is unacceptable: financial and service impact is high

Signals to generate if deviating:
- Red zone penetration on AX item → `critical` severity
- ADU zero on AX item → `critical` data quality
- Missing unit cost on AX item → `high` data quality

### AY — High value, variable demand

Recommended policy:
- Larger red zone protection relative to AX
- ADU review every 2–4 weeks
- VF should reflect demand variability (0.4–0.6 typical)
- Demand trend monitoring important
- Supplier reliability critical

Signals to generate if deviating:
- AY item with VF < 0.3 → buffer is undersized for variability, flag as `buffer_resizing`
- AY item with persistent red execution → `high`
- Low demand in last 30 days vs historical ADU → demand trend signal

### AZ — High value, highly variable demand

Recommended policy:
- Largest red zone, executive-level attention
- Consider demand shaping or pre-positioning
- MOQ constraints often cause overstock — check `items.moq` vs expected demand
- Frequent planner review required

Signals to generate:
- AZ item with small buffer (top_of_red < ADU × DLT) → `critical`
- AZ item with zero safety stock → `critical`
- AZ item with high excess AND MOQ > expected demand → MOQ negotiation recommendation

### BX — Medium value, stable demand

Recommended policy:
- Standard replenishment, moderate buffer
- VF typically 0.2–0.4
- Less frequent review than A items acceptable

Signals to generate:
- BX item in red execution for 2+ consecutive periods → `medium`
- BX item with large excess (>150% TOG) → `medium`

### BY — Medium value, variable demand

Recommended policy:
- Moderate buffer with variability protection
- Planner review on exception

Signals to generate:
- BY item with no buffer configured → `medium`
- BY item with VF < 0.2 → likely undersized

### BZ — Medium value, highly variable

Recommended policy:
- Consider reducing MOQ exposure
- Review if item should be made/ordered to demand
- Reduce stock investment

Signals to generate:
- BZ item with high inventory value and low/no demand last 60 days → `medium` overstock/obsolescence risk

### CX — Low value, stable demand

Recommended policy:
- Automate replenishment, minimal planning effort
- Simple min-max or DDMRP with large green zone acceptable
- Low-risk: stockout on C item is rarely critical

Signals: only generate if stockout is critical (item is flagged as critical in description
or production context). Otherwise suppress most CX signals.

### CY — Low value, variable demand

Recommended policy:
- Low-touch policy, bulk buying acceptable
- Consider consolidated orders

Signals: suppress unless execution color is `dark_red` or on_hand = 0

### CZ — Low value, highly variable

Recommended policy:
- Consider make-to-order or order-to-demand
- Minimize safety stock
- Review if item should remain in the portfolio

Signals:
- CZ item with high inventory value (despite low cost, high quantity) → flag excess
- CZ item with no demand in 90 days → potential obsolescence

---

## Portfolio-Level Analysis Rules

### Rule ABC-1 — Value Concentration

Calculate:
- What % of total inventory value is held in A items?
- What % of SKUs are A items?

If A items hold > 80% of value but represent < 15% of SKUs, state this as a
portfolio-level `portfolio` signal.

### Rule ABC-2 — Misaligned Segments

Identify items where the current buffer profile does not match the expected policy
for their ABC/XYZ class.

For each ABC/XYZ cell, the expected VF range is:
| Class | Expected VF range |
|---|---|
| AX | 0.10 – 0.30 |
| AY | 0.35 – 0.55 |
| AZ | 0.55 – 0.80 |
| BX | 0.20 – 0.40 |
| BY | 0.35 – 0.55 |
| BZ | 0.50 – 0.70 |
| CX | 0.20 – 0.40 |
| CY | 0.30 – 0.50 |
| CZ | 0.50 – 0.80 |

If `items.vf` is outside the expected range for the item's class, generate a
`abc_xyz_policy` signal recommending VF correction.

### Rule ABC-3 — AZ Items Requiring Executive Attention

If there are AZ-class items with execution_color in ('red', 'dark_red'), always
generate a dedicated `critical` severity portfolio signal summarizing all AZ items
at risk.

### Rule ABC-4 — C Items Over-Invested

Identify C-class items where inventory value > median inventory value of A items.
These C items represent disproportionate capital allocation.

---

## Output Format

signal_type: `abc_xyz_policy`

Examples:

```json
{
  "signal_type": "abc_xyz_policy",
  "severity": "critical",
  "part_number": "ITEM-007",
  "title": "AZ item with undersized buffer — high value at stockout risk [ITEM-007]",
  "detail": "ITEM-007 is classified AZ (annual value €42,000, CV = 1.34). Current top_of_red = 80 units = 6.4 days of ADU. For an AZ item with DLT = 21 days, the red zone should cover at least 10–15 days of demand. Current VF = 0.15 is well below the recommended 0.55–0.80 range for AZ items. The buffer provides insufficient protection against demand volatility.",
  "recommendation": "Increase VF from 0.15 to 0.60 for ITEM-007. Recalculate buffer zones. Expected new top_of_red ≈ 190 units. Review with planner and procurement to confirm supplier can support higher replenishment frequency before increasing buffer.",
  "metric_name": "variability_factor",
  "metric_value": 0.15,
  "metric_threshold": 0.55
}
```

```json
{
  "signal_type": "abc_xyz_policy",
  "severity": "info",
  "part_number": "PORTFOLIO",
  "title": "Portfolio concentration: 8 A-items hold 76% of total inventory value",
  "detail": "8 A-class items account for 76% of total inventory value (€234,000 of €308,000 total). These items deserve priority attention for buffer accuracy, supplier reliability, and demand monitoring. The remaining 47 items share only 24% of the value.",
  "recommendation": "Prioritize weekly planner review for the 8 A-class items. Ensure ADU is recalculated monthly. Confirm buffer zones are current. Implement supplier reliability tracking for their default suppliers.",
  "metric_name": "a_item_value_concentration_pct",
  "metric_value": 76,
  "metric_threshold": 70
}
```

### Title format

- Item-level: `"[ABC][XYZ] item — [specific deviation] [PART]"`
- Portfolio-level: `"Portfolio [finding type]: [quantified summary]"`

### Signal generation limits for this skill

- Maximum 1 portfolio-level summary signal
- Maximum 1 signal per AZ item in red/dark_red
- Maximum 5 VF misalignment signals (prioritize A items)
- Total maximum from this skill: 10 signals
