"""
One-off debug tool: prints the RAW JSON Upstox returns for expiries and
expired option contracts, so we can see the exact field names instead of
guessing from docs. Run this and paste me the full output.

    python debug_contracts.py
"""
import json
from config import load_upstox_creds, load_strategy_params
from upstox_client import UpstoxClient, UNDERLYING_INSTRUMENT_KEYS

creds = load_upstox_creds()
params = load_strategy_params()
client = UpstoxClient(creds)

print("=== 1. Fetching expiries list ===")
expiries = client.get_expiries(params.underlying)
print(f"Found {len(expiries)} expiries. First 5: {expiries[:5]}")

if not expiries:
    print("No expiries returned — stopping here, this itself is the problem.")
    raise SystemExit(1)

test_expiry = expiries[0]
print(f"\n=== 2. Fetching option contracts for expiry {test_expiry} ===")

# call the raw endpoint directly so we see the untouched response
raw = client._get("/v2/expired-instruments/option/contract", {
    "instrument_key": UNDERLYING_INSTRUMENT_KEYS[params.underlying],
    "expiry_date": test_expiry,
})
print("\nFull raw response (first 2 contracts shown in detail):")
data = raw.get("data", [])
print(f"Total contracts returned: {len(data)}")
print(json.dumps(data[:2], indent=2))

if data:
    print("\n=== Field names present on a single contract ===")
    print(list(data[0].keys()))
