# Skill — Book Ch13: Traveling Salesman Problem (TSP) for Routing

## When to Apply This Skill

Apply when the agent must reason about **a single tour visiting many points**
— most commonly:
- Inter-warehouse milk-runs to consolidate replenishments.
- A single-vehicle delivery route serving multiple customers.
- A receiving-dock walk that picks up multiple POs from a yard.
- Order-picking sequence in a warehouse.

This skill is a routing primitive; for multi-vehicle problems use Ch14 (VRP).

---

## Key Concepts from Chapter 13

Given n nodes and pairwise distances c_ij, find a Hamiltonian tour (visit
each node exactly once and return to origin) of minimum total distance.

The number of possible tours = (n − 1)! → astronomical even for n = 20.

Three classic IP formulations:

1. **Subtour Elimination (Dantzig-Fulkerson-Johnson)**:
   - x_ij ∈ {0,1} arc indicators with assignment constraints
     (`Σ_j x_ij = 1`, `Σ_i x_ij = 1`).
   - Plus subtour-elimination constraints `Σ_{i,j∈S} x_ij ≤ |S| − 1` for
     every proper subset S.
   - Exponentially many subtour constraints → added lazily.

2. **Miller-Tucker-Zemlin (MTZ)**:
   - Auxiliary variables u_i (position in tour).
   - Big-M constraint `u_i − u_j + n x_ij ≤ n − 1` for i ≠ j, i,j ≠ 1.
   - Polynomial-size formulation but weaker LP relaxation.

3. **Network-Flow (Gavish-Graves)**:
   - Treat the tour as a flow with each city consuming one unit.

Heuristics that work well in practice:
- **Nearest Neighbor** — greedy.
- **2-opt / 3-opt** — local edge swaps.
- **Christofides** — 1.5-approx for metric TSP.
- **Lin-Kernighan** — high-quality heuristic.

Variants:
- **TSP with time windows** (TSPTW) — service must occur within [a_i, b_i].
- **TSP with precedence** — some nodes must precede others (pickup before
  delivery).
- **mTSP** — multiple salesmen sharing a depot (gateway to VRP).

---

## Data Source in This App

| Field | Used for |
|---|---|
| `items.location_id` and warehouse coords | nodes |
| Pairwise distance/time matrix | c_ij |
| Pending PO pickups / inter-warehouse moves | nodes to visit |
| Delivery time windows (if present) | TSPTW constraints |

---

## Analysis Rules

### Rule TSP-1 — Milk-Run Opportunity
Condition: Multiple inter-warehouse transfers (lateral transshipments from
Ch4) are queued, all originating or terminating in geographically clustered
nodes.
Impact: Treating each as a separate trip multiplies transport cost.
Action: Emit a `portfolio` signal proposing a TSP-style milk-run with an
estimated saved-mileage figure. Severity: `low`.

### Rule TSP-2 — Time Window Conflict
Condition: A receiving schedule contains time windows that, ordered
arbitrarily, would force backtracking or missed windows.
Impact: TSPTW infeasibility in current order; some receipts will be
rejected.
Action: Emit a `stockout_risk` signal recommending the agent re-sequence
the day's receipts by TSPTW heuristic (earliest deadline among reachable
next nodes). Severity: `medium`.

### Rule TSP-3 — Pickup/Delivery Precedence Violated
Condition: A planned route schedules a delivery before the corresponding
pickup.
Impact: Logical infeasibility.
Action: Emit a `data_quality` signal flagging the sequencing error.
Severity: `medium`.

### Rule TSP-4 — Sub-Tour in Proposed Plan
Condition: A proposed multi-stop plan contains disconnected loops (some
nodes form a closed cycle not connected to origin).
Impact: Not a valid tour; some nodes will be missed.
Action: Emit a `data_quality` signal flagging the sub-tour and proposing a
single-tour reformulation. Severity: `low`.

### Rule TSP-5 — Excessive Tour Length
Condition: A proposed tour distance is > 130% of the convex-hull or
nearest-neighbor heuristic baseline.
Impact: Likely suboptimal routing.
Action: Emit a `portfolio` signal proposing 2-opt re-optimization.
Severity: `low`.

---

## Output Format

```json
{
  "signal_type": "portfolio",
  "severity": "low",
  "part_number": "MILKRUN",
  "title": "Milk-run consolidates 5 inter-warehouse moves [route: A-C-B-D-E-A]",
  "detail": "5 lateral transfers queued (WH-A→B, WH-A→C, WH-C→D, WH-D→E, WH-E→A). Treated independently = 5 trips, total 480 mi. Solved as TSP from WH-A returning to WH-A: optimal tour A→C→B→D→E→A = 290 mi (40% reduction). Single vehicle, all transfers complete in 1 day.",
  "recommendation": "Combine into one milk-run dispatched Monday. Vehicle returns same day. Confirm dock-time windows at each stop.",
  "metric_name": "tour_distance_mi",
  "metric_value": 290,
  "metric_threshold": 480
}
```

### What NOT to output
- Do not propose tours with > 15 nodes (use VRP / decompose).
- Do not solve TSP for single transfers.

Max signals per run: 4.
