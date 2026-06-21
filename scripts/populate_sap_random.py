"""One-off: populate the AS-IS SAP planning fields on every Item with
randomised-but-plausible values, so the Methodology Simulation has an AS-IS
baseline to replay. Values are derived from each part's ADU / DLT so they make
physical sense; MRP Type is drawn from a realistic mix.

Run:  DATABASE_URL=<supabase-url> python scripts/populate_sap_random.py
"""
import math
import random

from database import db as D

random.seed(42)

MRP_MIX = (["PD"] * 45 + ["VB"] * 25 + ["VM"] * 20 + ["ND"] * 10)


def _ceil_to(value, step):
    return math.ceil(value / step) * step if step > 0 else value


def main():
    D.init_db()  # ensure the 9 sap_* columns exist on this database
    s = D.SessionLocal()
    try:
        items = s.query(D.Item).all()
        updated = 0
        for it in items:
            adu = float(it.adu or 0.0)
            dlt = float(it.dlt or 0.0)
            mrp = random.choice(MRP_MIX)

            # Lead time split: GR 1-2d, planned delivery ≈ DLT (fallback 5-20d)
            grt = random.choice([1.0, 2.0])
            base_lt = round(dlt) if dlt > 0 else random.randint(5, 20)
            pdt = max(float(base_lt) - grt, 1.0)
            lead = pdt + grt

            it.sap_mrp_type = mrp
            it.sap_planned_delivery_time = pdt
            it.sap_gr_processing_time = grt

            if mrp == "ND":
                # Unmanaged — no planning parameters.
                it.sap_safety_stock = 0.0
                it.sap_reorder_point = 0.0
                it.sap_fixed_lot = 0.0
                it.sap_min_lot = 0.0
                it.sap_max_lot = 0.0
                it.sap_rounding_value = 0.0
            else:
                ss_days = random.uniform(3.0, 10.0)
                ss = round(adu * ss_days, 1) if adu > 0 else float(random.randint(5, 40))
                rop = round(adu * lead + ss, 1) if adu > 0 else float(random.randint(20, 120))
                rounding = float(random.choice([1, 5, 10]))
                lot = max(_ceil_to(adu * random.uniform(7.0, 21.0), rounding),
                          rounding) if adu > 0 else float(random.randint(20, 100))

                it.sap_safety_stock = ss
                it.sap_reorder_point = rop if mrp in ("VB", "VM") else 0.0
                # PD = deterministic/lot-for-lot → only a rounding/min lot;
                # VB/VM = reorder point with a fixed lot.
                it.sap_fixed_lot = lot if mrp in ("VB", "VM") else 0.0
                it.sap_min_lot = round(lot * 0.5, 1) if mrp in ("VB", "VM") else 0.0
                it.sap_max_lot = round(lot * 4.0, 1) if mrp in ("VB", "VM") else 0.0
                it.sap_rounding_value = rounding

            updated += 1

        s.commit()
        # Summary
        from collections import Counter
        mix = Counter(it.sap_mrp_type for it in items)
        print(f"Updated {updated} items. MRP Type mix: {dict(mix)}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
