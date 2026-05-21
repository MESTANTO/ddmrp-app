# Skill 09 — Demand Forecasting & Inventory Optimisation

## When to Apply This Skill

Apply this skill when the agent context contains a section labelled `DEMAND FORECAST DATA`.

This skill governs how to interpret demand history, identify items with stale ADU values,
detect seasonal patterns, evaluate forecast accuracy, and recommend actions that improve
buffer sizing and reduce carrying costs or stockout risk.

---

## Data Source in This App

All demand data originates from the `demand_entries` table filtered by `company_id`.

| Field | Table | Use |
|---|---|---|
| `id`, `part_number` | items | Item identity |
| `adu` | items | Currently stored Average Daily Usage |
| `dlt` | items | Decoupled Lead Time (days) |
| `variability_factor` (VF) | items | Should reflect demand CV |
| `unit_cost` | items | Financial impact of forecast errors |
| `top_of_red` | buffers | Safety stock target (DDMRP) |
| `quantity`, `demand_date` | demand_entries | Raw demand history |
| `demand_type` | demand_entries | Filter for `actual` records only |

The agent context provides pre-computed forecast statistics per item:

| Field | Meaning |
|---|---|
| `months_of_data` | Count of calendar months with actual demand |
| `mean_monthly` | Average monthly demand (units) |
| `std_monthly` | Standard deviation of monthly demand |
| `cv` | Coefficient of variation (σ / μ) — demand variability |
| `trend_slope` | Linear trend in demand (units/month, + = growing) |
| `sma3_forecast` | 3-month simple moving average forecast |
| `stored_adu` | Item's current ADU in master data |
| `implied_adu` | ADU implied by recent demand history (mean_monthly / 30) |
| `adu_divergence_pct` | % gap between implied_adu and stored_adu |
| `has_seasonality` | Boolean — detected seasonal pattern (≥12 months data) |
| `peak_months` | Months with seasonal index ≥ 1.2 |
| `trough_months` | Months with seasonal index ≤ 0.8 |

---

## Forecasting Methods Available in This App

The chat agent can call these tools when the user requests deeper analysis:

| Tool | Method | Best For |
|---|---|---|
| `forecast_item_demand(item_id, method="sma_3")` | 3-month SMA | Stable demand |
| `forecast_item_demand(item_id, method="sma_6")` | 6-month SMA | Slow-moving items |
| `forecast_item_demand(item_id, method="wma")` | Weighted MA (45/35/20%) | Recent-trend sensitive |
| `forecast_item_demand(item_id, method="seasonal")` | Seasonal-adjusted SMA | Strong seasonal items |
| `forecast_item_demand(item_id, method="trend")` | Linear trend projection | Growing/declining demand |
| `item_seasonality_analysis(item_id)` | Seasonal indices | Identify peak/trough months |
| `forecast_accuracy_report(item_id)` | Walk-forward MAE/MAPE | Measure forecast quality |

---

## Analysis Framework

### 1. Identify Stale ADU (highest priority)

Flag items where `|adu_divergence_pct|` is large:
- **> 30%**: ADU is significantly out of date. Buffer zones (TOR/TOY/TOG) are incorrectly sized.
  Recommend `propose_apply_item_params(item_id, reason=...)` to queue an ADU update.
- **15–30%**: Moderate divergence. Recommend reviewing and optionally updating.
- **< 15%**: ADU is current. No action needed.

When ADU is understated (implied_adu > stored_adu): item is more likely to hit stockouts.
When ADU is overstated (implied_adu < stored_adu): item is carrying excess safety buffer.

### 2. Assess Demand Variability

Use `cv` (coefficient of variation):
- **CV < 0.3**: Low variability (XYZ = X class). Standard buffer sizing is appropriate.
- **CV 0.3–0.7**: Moderate variability. Variability Factor (VF) should reflect this.
- **CV > 0.7**: High variability (XYZ = Z class). Recommend reviewing VF and considering
  a buffer adjustment to temporarily increase the Red Zone.

### 3. Detect Seasonal Patterns

If `has_seasonality = true`:
- Identify `peak_months` and `trough_months` from the context.
- For items entering a peak season in the next 1–2 months: recommend a Buffer Adjustment
  (DAF > 1.0) to increase ADU × DLT product during peak. Use `propose_create_buffer_adjustment`.
- For items in trough months with excess stock: flag overstock risk.

### 4. Detect Demand Trend

Use `trend_slope`:
- **Slope > 1.0 units/month (> 2% monthly growth)**: Growing demand. Stored ADU is likely
  understated. Recommend ADU update and consider raising TOG.
- **Slope < -1.0 units/month**: Declining demand. ADU may be overstated, leading to
  excess inventory. Recommend reviewing and potentially downward ADU adjustment.
- **|Slope| < 1.0**: Stable demand. No trend action needed.

### 5. Evaluate Forecast Method Fit

Choose the right method per item profile:
- Stable demand + no trend → SMA-3
- High variability + recent event → WMA (more weight on recent)
- Seasonal pattern confirmed → seasonal-adjusted SMA
- Consistent growth/decline → trend projection

---

## Safety Stock / ROP Context (DDMRP equivalent)

In DDMRP, the Red Zone base = ADU × DLT × VF plays the role of safety stock.
The Safety Stock formula reference:

```
Safety Stock = Z × σ_demand × √(DLT/30)
ROP = (ADU × DLT) + Safety Stock
```

If the statistical safety stock (from `calculate_item_safety_stock`) significantly
exceeds the DDMRP Top of Red:
- Consider increasing VF or adding a ZAF (Zone Adjustment Factor) via `propose_create_buffer_adjustment`.

If Top of Red is much larger than statistical safety stock:
- Item may be over-buffered; consider reducing VF.

---

## EOQ vs DDMRP Order Quantity

DDMRP replenishes to TOG — NFP (Net Flow Position). EOQ is not directly used.
However, if the DDMRP suggested order quantity is far from EOQ, it may indicate
min_order_qty (MOQ) or order_cycle settings need updating.
Flag items where `suggested_order_qty / eoq` is outside the range [0.5, 2.0].

---

## Forecast Accuracy Standards

| MAPE | Status | Action |
|---|---|---|
| < 10% | Good | No action needed |
| 10–20% | Acceptable | Monitor; review method choice |
| > 20% | Poor | Change forecast method; investigate demand drivers |

Systematic bias (bias > 0 = over-forecast, bias < 0 = under-forecast) is more
actionable than MAPE alone. An item with 15% MAPE and consistent under-forecast
bias is more dangerous (stockout-prone) than one with 20% MAPE and no bias.

---

## Output Rules for This Skill

Use `signal_type` values:
- `demand_trend` — for ADU divergence, trend, CV, seasonality signals
- `buffer_resizing` — when buffer adjustment (DAF/VF) is recommended
- `stockout_risk` — when demand is growing and ADU is understated
- `overstock` — when demand is declining and ADU is overstated
- `data_quality` — when an item has too little demand history to forecast reliably
- `portfolio` — for company-wide patterns (e.g., "40% of items have ADU divergence > 20%")

Every recommendation must:
1. Name the specific item (part_number)
2. State the action: update ADU, add buffer adjustment, change VF, etc.
3. Quantify the impact: "ADU 0.42 → 0.67 (+60%); buffer zones will increase ~43%"
4. For buffer adjustments: specify the factor and date range

Generate 3–12 signals focused on the most impactful items. Prioritise:
1. Items with large ADU divergence + high unit_cost (financial impact)
2. Items entering peak season with no buffer adjustment
3. Items with growing trend + current stockout risk (red/dark_red execution)
4. Items with high CV that need VF update
