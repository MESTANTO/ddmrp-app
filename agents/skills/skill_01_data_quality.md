# Skill 01 — Data Quality Assessment

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `DATA QUALITY ISSUES`.
This skill governs how to interpret missing, inconsistent, or unreliable master data and
how to communicate that clearly as signals.

Do not skip data quality. Poor data produces incorrect recommendations. Always surface
data issues before operational findings.

---

## Data Source in This App

All fields below come from the `items` table, filtered by `company_id`.
Buffer fields come from the `buffers` table joined on `item_id`.
Supplier fields come from the `suppliers` table joined via `items.default_supplier_id`.

### Item fields relevant to this skill

| Field | Meaning | Quality check |
|---|---|---|
| `part_number` | Unique item identifier | Must never be blank |
| `unit_cost` | Standard cost in company currency | Zero or NULL = financial analysis impossible |
| `adu` | Average daily usage (units/day) | Zero while demand history exists = ADU not calculated |
| `dlt` | Decoupled lead time (days) | Zero or NULL for a buffered item = buffer sizing invalid |
| `ltf` | Lead time factor (0.1–1.0 typical) | NULL = buffer red zone cannot be calculated |
| `vf` | Variability factor (0.1–1.0 typical) | NULL = buffer yellow/green zones unreliable |
| `moq` | Minimum order quantity | Zero or NULL for purchased items = ordering logic broken |
| `item_type` | M / I / P / D | NULL = planning policy cannot be assigned |
| `default_supplier_id` | FK to suppliers | NULL for P (Purchased) items = procurement risk |
| `spike_threshold` | Order spike threshold | Zero for buffered item = all demand treated as spike |

### Buffer fields relevant to this skill

| Field | Meaning | Quality check |
|---|---|---|
| `top_of_red` | Top of red zone | Zero or NULL for a buffered item = protection missing |
| `top_of_yellow` | Top of yellow zone | Must be > top_of_red |
| `top_of_green` | Top of green zone | Must be > top_of_yellow |
| `net_flow_position` | On-hand + on-order − qualified demand | NULL = NFP cannot be evaluated |
| `execution_color` | red / yellow / green / dark_red | NULL = execution signal missing |
| `buffer_status_pct` | On-hand as % of TOR | NULL or negative = buffer health unknown |

---

## Analysis Rules

### Rule DQ-1 — Missing Unit Cost

Condition: `items.unit_cost IS NULL OR items.unit_cost <= 0`

Impact: Financial opportunity sizing (excess value, shortage value, cash release) is
impossible without unit cost. All financial KPIs for this item are unreliable.

Severity:
- A-class item (top 70% of annual value): `high`
- B or C-class item: `medium`
- Item with buffer and zero cost: `high` (buffer value unknown)

### Rule DQ-2 — Zero ADU with Demand History

Condition: `items.adu = 0` AND demand entries exist for this item in `demand_entries`
within the last 90 days.

Impact: Buffer zones are sized on zero demand. The buffer is effectively misconfigured.
NFP calculations are meaningless. This item will never trigger a replenishment signal.

Severity: `high` if the item has a buffer. `medium` otherwise.

### Rule DQ-3 — Missing or Zero DLT on Buffered Item

Condition: `items.dlt = 0 OR items.dlt IS NULL` AND a buffer row exists for this item.

Impact: The yellow zone (ADU × DLT) equals zero. The buffer has no coverage for the
replenishment cycle. This is a critical configuration error.

Severity: `high`

### Rule DQ-4 — Buffer Zone Inconsistency

Condition: `buffers.top_of_red >= buffers.top_of_yellow`
OR `buffers.top_of_yellow >= buffers.top_of_green`
OR any zone value is zero or negative.

Impact: Buffer zones are logically impossible. Execution color and NFP comparisons
produce incorrect results. The item is effectively unmanaged.

Severity: `critical`

### Rule DQ-5 — Missing Supplier for Purchased Item

Condition: `items.item_type = 'P'` AND `items.default_supplier_id IS NULL`

Impact: No supplier lead time, reliability, or MOQ data is available. Buffer sizing
cannot be validated. Replenishment cannot be routed.

Severity: `medium`

### Rule DQ-6 — Lead Time Factor or Variability Factor Missing

Condition: `items.ltf IS NULL OR items.vf IS NULL` AND buffer row exists.

Impact: Buffer zones calculated without LTF/VF are based on defaults only. The red
zone protection and green zone replenishment cycle may be wrong for this item's
actual behavior.

Severity: `medium`

### Rule DQ-7 — Reliable Data Scoring

Before issuing operational recommendations on an item, score its data reliability:

- **High reliability**: unit_cost > 0, adu > 0, dlt > 0, ltf set, vf set, buffer zones
  consistent. Recommendations can be automated.
- **Medium reliability**: 1–2 fields missing. Recommendations require planner review.
- **Low reliability**: 3+ fields missing or zones inconsistent. Do not issue operational
  recommendations. Issue only data quality signals.

---

## Output Format

Each finding must be output as a JSON signal object with these exact fields:

```json
{
  "signal_type": "data_quality",
  "severity": "high",
  "part_number": "ITEM-001",
  "title": "Missing unit cost — financial analysis blocked [ITEM-001]",
  "detail": "Item ITEM-001 has unit_cost = 0. Excess value, shortage value, and cash-release opportunity cannot be calculated. All financial signals for this item are suppressed until cost is corrected.",
  "recommendation": "Assign the correct standard cost in the Item Master. Owner: cost accounting. Priority: before next agent run.",
  "metric_name": "unit_cost",
  "metric_value": 0,
  "metric_threshold": 0
}
```

### Title format

`[Rule short name] — [specific impact] [PART_NUMBER]`

Examples:
- `"Missing unit cost — financial analysis blocked [ITEM-001]"`
- `"ADU is zero with active demand — buffer never triggers [PART-XYZ]"`
- `"Buffer zones inconsistent: TOY < TOR — execution color invalid [ABC-123]"`

### Severity rules summary

| Condition | Severity |
|---|---|
| Buffer zones inconsistent | `critical` |
| Zero ADU with demand + buffer | `high` |
| Missing unit cost on A item | `high` |
| Zero DLT on buffered item | `high` |
| Missing unit cost on B/C item | `medium` |
| Missing supplier on P item | `medium` |
| Missing LTF/VF | `medium` |

### What NOT to output

Do not output a data_quality signal if:
- The item has no buffer and no demand history (it may be a new or inactive item)
- The missing field does not affect any active analysis (e.g. missing MOQ on an M item)
- The issue is already flagged by another signal in the same run

Maximum data_quality signals per run: 15. Prioritize by severity then by inventory value.
