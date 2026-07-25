"""
Thin wrapper around Upstox's REST API (v2/v3), using plain `requests` so
there's no hidden SDK behavior to debug.

Docs referenced:
  https://upstox.com/developer/api-documentation/expired-instruments/
  https://upstox.com/developer/api-documentation/get-expired-historical-candle-data/
  https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
  https://upstox.com/developer/api-documentation/place-order/ (v3, live phase)

IMPORTANT: Upstox's Expired Instruments APIs live under the "Upstox Plus"
plan. Activate Plus (currently free) at developer.upstox.com before using
this — otherwise these calls will 4xx just like Dhan's did without the Data
API subscription.
"""
import time
import requests
from config import UpstoxCreds

BASE_URL = "https://api.upstox.com"

UNDERLYING_INSTRUMENT_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}


class UpstoxClient:
    def __init__(self, creds: UpstoxCreds):
        self.creds = creds
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Authorization": f"Bearer {creds.access_token}",
        })

    # ---------- expired options data ----------

    def get_expiries(self, underlying: str) -> list[str]:
        """All available expired-contract expiry dates for the underlying
        (currently ~6 months of history per Upstox Plus)."""
        params = {"instrument_key": UNDERLYING_INSTRUMENT_KEYS[underlying]}
        resp = self._get("/v2/expired-instruments/expiries", params)
        return sorted(resp.get("data", []))

    def get_expired_option_contracts(self, underlying: str, expiry_date: str) -> list[dict]:
        """Returns list of {strike_price, instrument_type (CE/PE), expired_instrument_key, ...}
        for every strike available at that expiry."""
        params = {
            "instrument_key": UNDERLYING_INSTRUMENT_KEYS[underlying],
            "expiry_date": expiry_date,
        }
        resp = self._get("/v2/expired-instruments/option/contract", params)
        return resp.get("data", [])

    def get_expired_historical_candles(self, expired_instrument_key: str, interval: str,
                                        to_date: str, from_date: str) -> dict:
        """interval: '1minute' | '3minute' | '5minute' | '15minute' | '30minute' | 'day'"""
        # instrument key contains '|' which must stay literal in the path per docs' own examples
        path = f"/v2/expired-instruments/historical-candle/{expired_instrument_key}/{interval}/{to_date}/{from_date}"
        return self._get(path)

    # ---------- live/underlying data ----------

    def get_spot_history(self, underlying: str, unit: str, interval: int,
                          to_date: str, from_date: str) -> dict:
        """Uses the v3 historical-candle endpoint for the index itself
        (used to compute the ATM strike at entry time)."""
        key = UNDERLYING_INSTRUMENT_KEYS[underlying]
        path = f"/v3/historical-candle/{key}/{unit}/{interval}/{to_date}/{from_date}"
        return self._get(path)

    # ---------- LIVE (non-expired, tradable) option data ----------
    # NEW in this revision. IMPORTANT FIX: strangle_data_fetcher.fetch_live_chain
    # was previously calling get_expired_option_contracts() even in live mode --
    # that returns EXPIRED instrument keys, which cannot be quoted/traded live.
    # These two methods hit the live contract endpoint instead. Verify these
    # exact paths against current Upstox API docs before going live -- endpoint
    # shapes do drift; this is flagged here rather than silently assumed correct.

    def get_live_expiries(self, underlying: str) -> list[str]:
        """Live option contracts endpoint, no expiry_date filter -> returns
        contracts across the near expiries; we collect the distinct expiry
        dates present. Mirrors get_expiries() but for tradable (not expired)
        contracts."""
        params = {"instrument_key": UNDERLYING_INSTRUMENT_KEYS[underlying]}
        resp = self._get("/v2/option/contract", params)
        data = resp.get("data", [])
        expiries = sorted({c["expiry"] for c in data if "expiry" in c})
        return expiries

    def get_live_option_contracts(self, underlying: str, expiry_date: str) -> list[dict]:
        """Live, tradable option contracts for one expiry -- instrument_key
        here is a real live instrument that get_live_ltp() / place_order()
        can act on (unlike get_expired_option_contracts())."""
        params = {
            "instrument_key": UNDERLYING_INSTRUMENT_KEYS[underlying],
            "expiry_date": expiry_date,
        }
        resp = self._get("/v2/option/contract", params)
        return resp.get("data", [])

    def get_live_ltp(self, instrument_keys: list[str]) -> dict:
        """Batched LTP quote for up to 500 instrument keys per call. Returns
        {instrument_key: ltp}."""
        out = {}
        for i in range(0, len(instrument_keys), 500):
            batch = instrument_keys[i:i + 500]
            resp = self._get("/v3/market-quote/ltp", {"instrument_key": ",".join(batch)})
            for k, v in resp.get("data", {}).items():
                out[k] = v.get("ltp", 0.0)
        return out

    # ---------- order endpoints (live phase) ----------

    def place_order(self, **kwargs):
        """v3 place-order endpoint. Only call this from live_trader.py once
        DRY_RUN is deliberately turned off."""
        return self._post("/v3/order/place", kwargs)

    # ---------- internals ----------

    def _get(self, path: str, params: dict = None, retries: int = 3) -> dict:
        return self._request("GET", path, params=params, retries=retries)

    def _post(self, path: str, json_body: dict, retries: int = 3) -> dict:
        return self._request("POST", path, json_body=json_body, retries=retries)

    def _request(self, method: str, path: str, params: dict = None,
                 json_body: dict = None, retries: int = 3) -> dict:
        url = BASE_URL + path
        last_err = None
        for attempt in range(retries):
            resp = self.session.request(method, url, params=params, json=json_body, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            last_err = f"{resp.status_code}: {resp.text}"
            time.sleep(0.5)
        raise RuntimeError(f"Upstox API call to {path} failed: {last_err}")
