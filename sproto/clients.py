"""Thin, rate-limited clients for Etherscan (v2) and OpenSea (v2)."""
import os
import time

import requests

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
OPENSEA_URL = "https://api.opensea.io/api/v2"
PAGE = 1000  # free-tier Etherscan max rows per call


class Etherscan:
    def __init__(self, key=None, min_interval=0.36):
        self.key = key or os.environ["ETHERSCAN_API_KEY"]
        self.min_interval = min_interval
        self._last = 0.0
        self.calls = 0
        self.s = requests.Session()

    def _get(self, **params):
        params.update(chainid=1, apikey=self.key)
        for attempt in range(8):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                r = self.s.get(ETHERSCAN_URL, params=params, timeout=30)
                d = r.json()
            except (requests.RequestException, ValueError):
                time.sleep(2 * (attempt + 1))
                continue
            if "jsonrpc" in d:  # proxy module
                if "error" in d:
                    msg = str(d["error"])
                    if "rate" in msg.lower():
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise RuntimeError(f"etherscan proxy error: {msg}")
                return d.get("result")
            res = d.get("result")
            if d.get("status") == "1":
                return res
            msg = str(res) + " " + str(d.get("message"))
            if "No transactions found" in msg or "No records found" in msg or res == []:
                return []
            if "rate limit" in msg.lower():
                time.sleep(1.5 * (attempt + 1))
                continue
            if "daily limit" in msg.lower():
                raise RuntimeError("Etherscan daily limit reached")
            raise RuntimeError(f"etherscan error {params.get('action')}: {msg}")
        raise RuntimeError(f"etherscan retries exhausted: {params.get('action')}")

    def account_paged(self, action, startblock=0, endblock=99999999, **kw):
        """Fetch all rows for an account action, paging forward by block."""
        out, seen = [], set()
        while True:
            rows = self._get(module="account", action=action, startblock=startblock,
                             endblock=endblock, page=1, offset=PAGE, sort="asc", **kw)
            new = 0
            for x in rows:
                k = (x.get("hash"), x.get("tokenID"), x.get("from"), x.get("to"),
                     x.get("value"), x.get("tokenValue"), x.get("logIndex"), x.get("traceId"))
                if k in seen:
                    continue
                seen.add(k)
                out.append(x)
                new += 1
            if len(rows) < PAGE or new == 0:
                return out
            startblock = int(rows[-1]["blockNumber"])

    def first_rows(self, action, address, n=10):
        return self._get(module="account", action=action, address=address,
                         startblock=0, endblock=99999999, page=1, offset=n, sort="asc")

    def block_by_time(self, ts):
        return int(self._get(module="block", action="getblocknobytime",
                             timestamp=int(ts), closest="before"))

    def latest_block(self):
        return int(self._get(module="proxy", action="eth_blockNumber"), 16)

    def tx(self, h):
        return self._get(module="proxy", action="eth_getTransactionByHash", txhash=h)

    def code(self, address):
        return self._get(module="proxy", action="eth_getCode", address=address, tag="latest")

    def nonce(self, address):
        return int(self._get(module="proxy", action="eth_getTransactionCount",
                             address=address, tag="latest"), 16)

    def contract_creation(self, addresses):
        return self._get(module="contract", action="getcontractcreation",
                         contractaddresses=",".join(addresses))


class OpenSea:
    def __init__(self, key=None):
        self.key = key or os.environ["OPENSEA_API_KEY"]
        self.s = requests.Session()
        self.s.headers.update({"x-api-key": self.key, "accept": "application/json"})

    def get(self, path, **params):
        for attempt in range(6):
            r = self.s.get(f"{OPENSEA_URL}{path}", params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            time.sleep(0.25)
            return r.json()
        raise RuntimeError(f"opensea retries exhausted: {path}")

    def sales_since(self, slug, after_ts):
        out, nxt = [], None
        while True:
            p = dict(event_type="sale", limit=50, after=int(after_ts))
            if nxt:
                p["next"] = nxt
            d = self.get(f"/events/collection/{slug}", **p)
            out += d.get("asset_events", [])
            nxt = d.get("next")
            if not nxt:
                return out
