# Skill — Book Ch14: Vehicle Routing Problem (VRP) & Inventory Routing

## When to Apply This Skill

Apply when multiple vehicles (or shipment lanes, or planner teams) must serve
multiple destinations under capacity constraints. The most DDMRP-relevant
variant is the **Inventory Routing Problem (IRP)**, which jointly optimizes
replenishment quantity, timing, and route — directly extending DDMRP buffer
execution to the distribution layer.

Triggers:
- A network with > 1 vehicle and > 5 destinations.
- DDMRP buffer alerts at multiple downstream nodes served by a shared fleet.
- Customer-managed inventory or vendor-managed inventory (VMI) scenarios.

---

## Key Concepts from Chapter 14

**Capacitated VRP (CVRP)**:

Given:
- Fleet K of homogeneous vehicles, each with capacity Q.
- Customers N with demand q_i.
- A single depot 0; vehicles start and end there.
- Travel cost c_ij.

Find |K| tours minimizing total cost, satisfying:
- Each customer visited exactly once.
- Vehicle capacity respected.
- Subtour-elimination valid.

VRP variants:
- **VRPB** (with backhaul) — deliveries first, pickups after.
- **VRPPD** — paired pickup-and-delivery requests.
- **PVRP** — periodic over multiple days with visit patterns.
- **IRP** — **inventory routing**: deliver enough to each customer to
  prevent stock-out, minimize travel + holding cost across the horizon.
- **SDVRP** — split delivery (multiple vehicles can serve same customer).

**Inventory Routing Problem (IRP)** is the most DDMRP-aligned:
- Each customer has known demand rate d_i and storage cap U_i.
- Vehicle visits replenish toward U_i.
- Objective: minimize total travel + holding cost subject to no stockouts.
- This is **the math behind VMI** — the supplier becomes the buffer manager
  for the downstream node.

Compact, MTZ, and flow-based formulations are direct extensions of the TSP
formulations from Ch13.

---

## Data Source in This App

| Field | Used for |
|---|---|
| Multi-location buffers (same item, different `location_id`) | customer nodes |
| `items.adu` per location | demand rate d_i |
| `buffers.top_of_green` per location | storage cap U_i |
| `buffers.on_hand_qty` per location | current inventory |
| Vehicle capacity (snapshot) | Q |

---

## Analysis Rules

### Rule VRP-1 — Independent Routes Per Item (VRP Opportunity)
Condition: Multiple buffer-replenishment trips are scheduled to overlapping
customer nodes on the same day, each carrying a single item.
Impact: Vehicle capacity underutilized; total cost higher than CVRP optimal.
Action: Emit a `portfolio` signal proposing combined CVRP routing.
Severity: `low`.

### Rule VRP-2 — IRP Replenishment Imbalance
Condition: At a downstream node, on-hand at the time of vehicle arrival is
either > 95% of cap (wasted trip) or < 5% (near-stockout).
Impact: IRP would shift the visit calendar to smooth the deliveries.
Action: Emit a `buffer_resizing` signal recommending revised PVRP visit
pattern (or VMI parameters). Severity: `medium`.

### Rule VRP-3 — Capacity Infeasibility
Condition: Total demand assigned to one vehicle on one route exceeds vehicle
capacity Q.
Impact: Solution infeasible; some load drops.
Action: Emit a `data_quality` signal proposing split delivery (SDVRP) or
adding a second vehicle to the route. Severity: `high`.

### Rule VRP-4 — Backhaul Opportunity Missed
Condition: Vehicles return empty from delivery while suppliers along the
return route have pickup-ready POs.
Impact: Half the vehicle-miles are unproductive.
Action: Emit a `portfolio` signal proposing VRPB-style backhaul pairing.
Severity: `low`.

### Rule VRP-5 — Periodic Pattern Mismatch
Condition: A customer's required service frequency (from ADU and capacity)
doesn't match its assigned PVRP visit pattern.
Impact: Either chronic stock-out or chronic over-trip.
Action: Emit a `buffer_resizing` signal proposing the closest PVRP pattern.
Severity: `medium`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "medium",
  "part_number": "IRP-PATTERN",
  "title": "IRP rebalance — 3 customers visited too early [route: Depot-C1-C2-C3]",
  "detail": "Customers C1, C2, C3 each store ITEM-V004 at top-of-green 1,200 units. Current PVRP pattern visits Mondays. Avg on-hand at arrival: C1=1,140 (95% full → wasted trip), C2=380 (32% — sweet spot), C3=60 (5% — near stockout). IRP would visit C1 less often and C3 more often.",
  "recommendation": "Re-time visits: C1 every other Monday; C2 every Monday; C3 Monday + Thursday. Re-evaluate after 60 days. Net trips/month: 6 → 7 but no stockouts.",
  "metric_name": "max_stockout_risk_pct",
  "metric_value": 95,
  "metric_threshold": 30
}
```

### What NOT to output
- Do not propose VRP changes without multi-customer data.
- Do not solve VRP for snapshots with < 5 stops.

Max signals per run: 4.
