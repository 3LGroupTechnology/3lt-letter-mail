"""Pricing sanity checks: flat $1.00 per letter on top of base cost."""
import pricing

BASE = 0.97

# single letter
q = pricing.quote(BASE, 1)
assert q["tier"] == "flat", q
assert q["service_fee_per_letter"] == 1.00, q
assert q["estimated_total"] == round(0.97 + 1.00, 2) == 1.97, q

# quantity scales linearly — no tiers
q = pricing.quote(BASE, 9)
assert q["tier"] == "flat", q
assert q["service_fee_total"] == 9.00, q
assert q["estimated_total"] == round(9 * 0.97 + 9.00, 2) == 17.73, q

# 10 letters: still flat $1 each
q = pricing.quote(BASE, 10)
assert q["tier"] == "flat", q
assert q["service_fee_per_letter"] == 1.00, q
assert q["service_fee_total"] == 10.00, q
assert q["estimated_total"] == round(10 * 0.97 + 10.00, 2) == 19.70, q

# large run
q = pricing.quote(BASE, 100)
assert q["service_fee_total"] == 100.00, q
assert q["estimated_total"] == round(100 * 0.97 + 100.00, 2) == 197.00, q

# custom base cost flows through
q = pricing.quote(2.50, 10)
assert q["base_total"] == 25.00 and q["estimated_total"] == 35.00, q

# invalid quantity rejected
try:
    pricing.quote(BASE, 0)
    raise AssertionError("quantity=0 should raise")
except ValueError:
    pass

print("pricing sanity checks passed ✔")
print("  1 letter :", pricing.quote(BASE, 1)["estimated_total"])
print(" 10 letters:", pricing.quote(BASE, 10)["estimated_total"], "(flat $1/letter)")
