# Skill 07 — Supplier Risk and Lead Time Analysis

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `SUPPLIER RISK`.

This skill governs how to identify items whose supply reliability, lead time, or
supplier configuration represents a risk to service levels, and how to recommend
appropriate buffer adjustments and procurement actions.

---

## Data Source in This App

Supplier data comes from the `suppliers` table, linked to items via
`items.default_supplier_id`.

| Field | Table | Use |
|---|---|---|
| `reliability_pct` | suppliers | On-time delivery rate (0–100) |
| `lead_time_days` | suppliers | Supplier's standard lead time |
| `name` | suppliers | Supplier name for output |
| `code` | suppliers | Supplier code for output |
| `dlt` | items | Decoupled lead time configured on item |
| `ltf` | items | Lead time factor |
| `vf` | items | Variability factor |
| `top_of_red` | buffers | Current safety protection |
| `execution_color` | buffers | Current execution status |
| `on_hand` | items | Physical stock |
| `unit_cost` | items | For financial exposure calculation |
| `item_type` | items | P = Purchased (most exposed to supplier risk) |

Pre-computed values available in context under `SUPPLIER RISK`:
- `part_number`
- `supplier_name`
- `reliability_pct`: from suppliers table
- `execution_color`: from buffers table

---

## Supplier Reliability Framework

Reliability thresholds used in this app:

| Reliability % | Classification | Buffer implication |
|---|---|---|
| ≥ 95% | High reliability | Standard LTF (0.5) acceptable |
| 85–94% | Moderate reliability | Consider LTF 0.6–0.7 |
| 70–84% | Low reliability | LTF 0.7–0.8 recommended |
| < 70% | Unreliable | LTF 0.8–1.0, escalation required |
| NULL / not set | Unknown | Flag as data quality — assume moderate risk |

The lead time factor (LTF) is the primary lever to compensate for supplier
unreliability in DDMRP. A higher LTF increases the red zone base, providing
more protection against late deliveries.

---

## Analysis Rules

### Rule SUP-1 — Unreliable Supplier + Active Execution Alarm

Condition: `suppliers.reliability_pct < 85`
AND `buffers.execution_color IN ('red', 'dark_red')`

The worst case: the item is in stockout or near-stockout AND the supplier
delivering it has a poor on-time record. The probability of the replenishment
arriving on time is low. Service risk is high.

Supplier delay probability = `(100 - reliability_pct) / 100`
Service impact: if the next order is late, the stockout will extend by
`items.dlt × supplier_delay_probability` days on average.

Actions:
- Expedite the current open order
- Request delivery confirmation from supplier
- Evaluate if an alternative supplier can be used
- Increase LTF to compensate for supplier unreliability going forward

Severity: `critical` for A-class items, `high` for B/C-class

### Rule SUP-2 — Low Reliability with Current LTF Mismatch

Condition: `suppliers.reliability_pct < 85`
AND `items.ltf < 0.7`

The supplier is unreliable but the lead time factor does not compensate for it.
The red zone is providing less protection than the supplier's track record requires.

Expected LTF for this reliability level:
- 85–90% reliability → LTF = 0.65
- 75–84% reliability → LTF = 0.75
- < 75% reliability → LTF = 0.85

Current LTF gap = `recommended_ltf - items.ltf`
Impact on TOR: `(recommended_ltf - current_ltf) × adu × dlt` units
Impact value: units × unit_cost

Severity:
- A-class item: `high`
- B-class item: `medium`
- C-class item: `low`

signal_type: `supplier_risk`

### Rule SUP-3 — Item DLT Lower than Supplier Lead Time

Condition: `items.dlt < suppliers.lead_time_days`

The decoupled lead time configured on the item is SHORTER than the supplier's
own lead time. This is logically impossible — the buffer will run out before a
new order can arrive. The buffer is fundamentally misconfigured.

DLT gap = `suppliers.lead_time_days - items.dlt` days
Impact: the yellow zone covers `adu × items.dlt` but the actual replenishment
takes `suppliers.lead_time_days`. There is an uncovered gap of `dlt_gap × adu` units.

Severity: `critical` — this is a configuration error that makes the buffer
mathematically unable to protect against stockout.

signal_type: `supplier_risk`

### Rule SUP-4 — Missing Supplier for Purchased Item

Condition: `items.item_type = 'P'` AND `items.default_supplier_id IS NULL`

No supplier is configured. Lead time data for the supplier cannot be validated.
The buffer is operating without any supplier accountability.

This is a data quality issue as well as a supply risk.

Severity: `medium`

signal_type: `supplier_risk`

### Rule SUP-5 — Supplier Concentration Risk (Portfolio Level)

Condition: Multiple A-class or B-class items share the same `default_supplier_id`
AND that supplier has `reliability_pct < 85`

If a single unreliable supplier is the default for multiple important items,
a supplier failure or delay affects several items simultaneously.

Count of A/B items with this supplier: N
Total inventory value at risk: sum of (on_hand × unit_cost) for those items

This is a portfolio-level signal (part_number = "PORTFOLIO").

Severity: `high` if N ≥ 3 AND reliability < 80%, otherwise `medium`

---

## LTF Recommendation Formula

When recommending a LTF change, use this formula:

  recommended_ltf = 1 - (reliability_pct / 100) + 0.5

Examples:
- 95% reliability → LTF = 1 - 0.95 + 0.5 = 0.55 ≈ 0.5 (round to 0.5)
- 85% reliability → LTF = 1 - 0.85 + 0.5 = 0.65
- 75% reliability → LTF = 1 - 0.75 + 0.5 = 0.75
- 65% reliability → LTF = 1 - 0.65 + 0.5 = 0.85

Always cap at 1.0. Do not recommend LTF < 0.5 for purchased items.

Impact of LTF change on TOR:
  new_tor = adu × dlt × (new_ltf + vf)
  delta_units = (new_ltf - old_ltf) × adu × dlt
  delta_value = delta_units × unit_cost

State this calculation in every supplier_risk signal that includes a LTF recommendation.

---

## Output Format

signal_type: `supplier_risk`

Examples:

```json
{
  "signal_type": "supplier_risk",
  "severity": "critical",
  "part_number": "ITEM-009",
  "title": "Unreliable supplier + red execution — late delivery very likely [ITEM-009]",
  "detail": "Supplier ABC Supplies (code: SUP-003) has reliability = 68%. Item ITEM-009 is currently in red execution (on_hand = 42 units, top_of_red = 95 units). The next replenishment order must arrive within 3.5 days (42 units / ADU 12 units/day) to avoid stockout. With 68% reliability, there is a 32% probability of a late delivery. Current LTF = 0.50 — insufficient for a 68% reliability supplier (recommended LTF = 0.82).",
  "recommendation": "1. Immediately contact ABC Supplies to confirm delivery date for the open order. 2. Request split delivery or expedited freight if lead time > 3 days. 3. After the immediate risk is resolved: increase LTF from 0.50 to 0.80 for ITEM-009. New TOR = 12 × 14 × (0.80 + 0.45) = 210 units (vs current 95 units). Evaluate alternative supplier qualification.",
  "metric_name": "supplier_reliability_pct",
  "metric_value": 68,
  "metric_threshold": 85
}
```

```json
{
  "signal_type": "supplier_risk",
  "severity": "critical",
  "part_number": "ITEM-027",
  "title": "DLT < supplier lead time — buffer cannot protect against stockout [ITEM-027]",
  "detail": "Item DLT = 10 days. Supplier Global Parts (SUP-007) lead time = 18 days. The yellow zone covers only ADU × 10 = 70 units of demand before the buffer expects replenishment. But the actual replenishment takes 18 days = 126 units of demand. Uncovered gap = 8 days × 9 units/day = 72 units (€1,440 at €20/unit). The buffer is configured on an incorrect DLT and will fail every replenishment cycle.",
  "recommendation": "Correct DLT for ITEM-027 from 10 to 18 days. Recalculate buffer zones immediately. New top_of_yellow ≈ 195 units (vs current 115 units). Also verify if the 18-day lead time from Global Parts is current — if lead time has reduced, update the supplier record.",
  "metric_name": "dlt_vs_supplier_lead_time_gap",
  "metric_value": -8,
  "metric_threshold": 0
}
```

```json
{
  "signal_type": "supplier_risk",
  "severity": "high",
  "part_number": "PORTFOLIO",
  "title": "Supplier concentration risk — 4 A/B items depend on unreliable SUP-003",
  "detail": "Supplier ABC Supplies (SUP-003, reliability = 68%) is the default supplier for 4 A/B-class items: ITEM-009, ITEM-015, ITEM-031, ITEM-044. Combined inventory value: €87,400. Combined annual consumption value: €312,000. A single supplier disruption affects all 4 items simultaneously. 3 of the 4 items have LTF < 0.70, providing insufficient protection for a 68% reliable supplier.",
  "recommendation": "1. Qualify at least one alternative supplier for the highest-value items (ITEM-009, ITEM-015). 2. Increase LTF to 0.80 for all 4 items. 3. Place this supplier on a procurement risk watch list. 4. Review supplier scorecard monthly until reliability improves above 90%.",
  "metric_name": "affected_items_count",
  "metric_value": 4,
  "metric_threshold": 2
}
```

### Title format

- Unreliable + alarm: `"Unreliable supplier + [color] execution — [risk description] [PART]"`
- LTF mismatch: `"Supplier reliability N% — LTF too low, buffer under-protected [PART]"`
- DLT < supplier LT: `"DLT < supplier lead time — buffer cannot protect against stockout [PART]"`
- Concentration: `"Supplier concentration risk — N A/B items depend on unreliable [SUPPLIER]"`

### Mandatory content in detail field

Always state:
- Supplier name and code
- Reliability % value
- Current LTF and recommended LTF (with formula)
- Financial exposure: on_hand × unit_cost for items at risk
- Specific days until stockout if in red execution

### Signal limits from this skill

Maximum 8 supplier_risk signals per run.
Priority: items in red/dark_red execution with unreliable suppliers first.
Then DLT mismatches. Then LTF gaps. Then portfolio concentration.
Suppress supplier_risk signals for C-class items unless DLT < supplier lead time
(configuration error that must always be fixed).
