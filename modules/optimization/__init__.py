"""
Deterministic optimization sessions for the DDMRP app.

Each session in this package implements a *pure-Python solver* for one
operations-research model class from Haitao Li's
"Optimization Modeling for Supply Chain Applications" (2023):

    ses01_sourcing      — Sourcing allocation MILP (Ch2 + Ch3)
    ses02_network       — Multi-echelon min-cost flow (Ch4)
    ses03_facility      — Facility location UFLP/CFLP (Ch7)
    ses04_decoupling    — Safety stock placement DP (Ch10)
    ses05_mps           — Master production schedule MILP (Ch8)
    ses06_scheduling    — Scheduling & rollout (Ch11 + Ch12)
    ses07_routing       — VRP / IRP (Ch13 + Ch14)
    ses08_finance       — Credit-term NLP (Ch15)

Solvers are deterministic and free (PuLP/CBC, NetworkX, OR-Tools, SciPy).
Results are persisted to `optimization_runs` / `optimization_results`
with a 10-day expiry.
"""
