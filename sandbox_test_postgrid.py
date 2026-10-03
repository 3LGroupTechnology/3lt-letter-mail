"""PostGrid sandbox validation: address verification + one test letter.

Usage: POSTGRID_API_KEY=test_sk_... .venv/bin/python sandbox_test_postgrid.py

Refuses live keys outright. With a test key PostGrid simulates everything:
no physical mail, no charges. Never prints the key.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import postgrid_client as pg

key = os.environ.get("POSTGRID_API_KEY", "")
if not key:
    sys.exit("POSTGRID_API_KEY is not set.")
if not key.startswith("test_"):
    sys.exit("REFUSING: not a test key. Live keys are never used by this script.")

print("test mode confirmed:", pg.is_test_mode())

addr = {
    "name": "3LT Sandbox",
    "address_line1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "postal_code": "94043",
    "country": "US",
}
try:
    v = pg.verify_address(addr)
    print("address verification:", v.get("status"))
except Exception as exc:  # AV is a separate PostGrid product; don't fail the run
    print("address verification unavailable:", exc)

letter = pg.create_letter(
    to=addr,
    from_address={
        "name": "3L Group Technology",
        "address_line1": "1562 E 108th St",
        "city": "Cleveland",
        "state": "OH",
        "postal_code": "44106",
        "country": "US",
    },
    html="<html><body><h1>3LT sandbox test</h1><p>If you're reading this, "
    "the rail works. This is a test letter — nothing was mailed.</p></body></html>",
    description="3LT PostGrid sandbox validation",
    idempotency_key="3lt-sandbox-validation-1",
)
print("letter id:", letter.get("id"))
print("letter status:", letter.get("status"))
print("SANDBOX OK — simulated only, nothing mailed, nothing charged.")
