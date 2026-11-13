"""Collect raw data for the Sproto Gremlins dashboard.

State lives in data/*.json so each run is incremental:
  transfers.csv   - every Sproto ERC-721 transfer (ownership is replayed from this)
  sales.json      - sale events from OpenSea's API (90-day backfill, then incremental)
  wallets.json    - per-wallet age / first funding source (immutable, cached forever)
  funders.json    - per-funder nonce + contract check (to detect exchanges/services)
  airdrops.json   - per-holder inbound unsolicited token transfers + outbound moves
  contracts.json  - creation time of token contracts seen in airdrops
"""
import json
import os
import sys
import time
from pathlib import Path

from sproto.clients import Etherscan, OpenSea

SLUG = "sproto-gremlins"
CONTRACT = "0xeeca64ea9fcf99a22806cd99b3d29cf6e8d54925"
ZERO = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
DATA = Path(__file__).parent / "data"
BACKFILL_DAYS = 90
DAY = 86400

# Known marketplace contracts (tx `to` address) -> label
MARKETS = {
    "0x0000000000000068f116a894984e2db1123eb395": "OpenSea",   # Seaport 1.6
    "0x00000000000000adc04c56bf30ac9d3c0aaf14dc": "OpenSea",   # Seaport 1.5
    "0x00000000000001ad428e4906ae43d8f9852d0dd6": "OpenSea",   # Seaport 1.4
    "0x000000000000ad05ccc4f10045630fb830b95127": "Blur",
    "0xb2ecfe4e4d61f8790bbb9de2d1259b9e2410cea5": "Blur",
    "0x39da41747a83aee658334415666f3ef92dd0d541": "Blur",      # Blur swap/aggregator
    "0x29469395eaf6f95920e59f858042f0e28d98a20b": "Blur",      # Blend
}
ETH_LIKE = {"ETH", "WETH", "BETH"}


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name, obj):
    DATA.mkdir(exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=0, sort_keys=True))  # line-per-item: small git diffs
    tmp.replace(DATA / name)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- transfers
def load_transfers():
    p = DATA / "transfers.csv"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines()[1:]:
        b, t, h, tok, f, to = line.split(",")
        out.append([int(b), int(t), h, tok, f, to])
    return out


def save_transfers(rows):
    DATA.mkdir(exist_ok=True)
    tmp = DATA / "transfers.csv.tmp"
    tmp.write_text("block,ts,tx,token,from,to\n" + "".join(",".join(map(str, r)) + "\n" for r in rows))
    tmp.replace(DATA / "transfers.csv")


def collect_transfers(es):
    rows = load_transfers()
    st = {"last_block": rows[-1][0] if rows else 0}
    keys = {(r[2], r[3], r[4], r[5]) for r in rows}
    start = max(0, st["last_block"] - 5)  # small reorg overlap
    new = es.account_paged("tokennfttx", contractaddress=CONTRACT, startblock=start)
    added = 0
    for x in new:
        r = [int(x["blockNumber"]), int(x["timeStamp"]), x["hash"], x["tokenID"],
             x["from"].lower(), x["to"].lower()]
        k = (r[2], r[3], r[4], r[5])
        if k not in keys:
            keys.add(k)
            rows.append(r)
            added += 1
    rows.sort(key=lambda r: (r[0], r[2]))
    if rows:
        st["last_block"] = rows[-1][0]
    save_transfers(rows)
    log(f"transfers: +{added}, total {len(rows)}, last block {st['last_block']}")
    return rows


def holders_from(rows):
    owner = {}
    for r in rows:
        owner[r[3]] = r[5]
    counts = {}
    for tok, a in owner.items():
        if a not in ZERO:
            counts[a] = counts.get(a, 0) + 1
    return owner, counts


# -------------------------------------------------------------------- sales
def collect_sales(os_, es):
    st = load("sales.json", {"last_ts": 0, "sales": [], "tx_to": {}})
    now = time.time()
    after = max(st["last_ts"] - 3600, now - BACKFILL_DAYS * DAY) if st["last_ts"] else now - BACKFILL_DAYS * DAY
    events = os_.sales_since(SLUG, after)
    have = {(s["tx"], s["token"]) for s in st["sales"]}
    added = 0
    for e in events:
        nft = e.get("nft") or {}
        if (nft.get("contract") or "").lower() != CONTRACT:
            continue
        key = (e["transaction"], nft.get("identifier"))
        if key in have:
            continue
        have.add(key)
        pay = e.get("payment") or {}
        qty = max(int(e.get("quantity") or 1), 1)
        try:
            price = int(pay.get("quantity", 0)) / 10 ** int(pay.get("decimals", 18)) / qty
        except (TypeError, ValueError):
            price = None
        st["sales"].append({
            "tx": e["transaction"], "token": nft.get("identifier"), "ts": e["event_timestamp"],
            "seller": (e.get("seller") or "").lower(), "buyer": (e.get("buyer") or "").lower(),
            "price": price, "symbol": pay.get("symbol"),
            "price_eth": price if pay.get("symbol") in ETH_LIKE else None,
            "protocol": (e.get("protocol_address") or "").lower(),
        })
        added += 1
    # label marketplace
    for s in st["sales"]:
        if s.get("market"):
            continue
        if s["protocol"] in MARKETS:
            s["market"] = MARKETS[s["protocol"]]
            continue
        to = st["tx_to"].get(s["tx"])
        if to is None:
            t = es.tx(s["tx"]) or {}
            to = (t.get("to") or "").lower()
            st["tx_to"][s["tx"]] = to
        s["market"] = MARKETS.get(to) or ("Blur" if s["symbol"] == "BETH" else f"other:{to[:10]}")
    st["sales"].sort(key=lambda s: s["ts"])
    if st["sales"]:
        st["last_ts"] = st["sales"][-1]["ts"]
    save("sales.json", st)
    log(f"sales: +{added}, total {len(st['sales'])}")


# ------------------------------------------------------------------ wallets
def wallet_info(es, addr):
    txs = es.first_rows("txlist", addr, 20)
    itx = es.first_rows("txlistinternal", addr, 20)
    ts_all = [int(x["timeStamp"]) for x in txs + itx]
    info = {"first_ts": min(ts_all) if ts_all else None, "funder": None,
            "fund_ts": None, "fund_via": None, "fund_eth": None}
    cands = []
    for x in txs:
        if x["to"].lower() == addr and int(x["value"]) > 0 and x.get("isError", "0") == "0":
            cands.append((int(x["timeStamp"]), x["from"].lower(), None, x["hash"], int(x["value"])))
            break
    for x in itx:
        if x["to"].lower() == addr and int(x["value"]) > 0 and x.get("isError", "0") == "0":
            cands.append((int(x["timeStamp"]), None, x["from"].lower(), x["hash"], int(x["value"])))
            break
    if cands:
        ts, frm, via, h, val = min(cands, key=lambda c: c[0])
        if frm is None:  # internal transfer: attribute to the tx initiator
            t = es.tx(h) or {}
            frm = (t.get("from") or via or "").lower()
        info.update(funder=frm, fund_ts=ts, fund_via=via, fund_eth=val / 1e18)
    code = es.code(addr) or "0x"
    info["is_contract"] = code not in ("0x", "0x0", "")
    return info


def collect_wallets(es, holders):
    wallets = load("wallets.json", {})
    todo = [a for a in holders if a not in wallets]
    log(f"wallets: {len(todo)} new to look up")
    for i, a in enumerate(todo):
        wallets[a] = wallet_info(es, a)
        if i % 50 == 49:
            save("wallets.json", wallets)
            log(f"  wallets {i + 1}/{len(todo)} (calls {es.calls})")
    save("wallets.json", wallets)

    funders = load("funders.json", {})
    need = {w["funder"] for a, w in wallets.items() if a in holders and w.get("funder")} - set(funders)
    log(f"funders: {len(need)} new to look up")
    for i, f in enumerate(sorted(need)):
        code = es.code(f) or "0x"
        funders[f] = {"nonce": es.nonce(f), "is_contract": code not in ("0x", "0x0", "")}
        if i % 100 == 99:
            save("funders.json", funders)
    save("funders.json", funders)


# ----------------------------------------------------------------- airdrops
SPAM_HINTS = ("http", "www", ".com", ".io", ".org", ".xyz", ".net", "claim", "visit", "reward", "t.me", "$")


def collect_airdrops(es, holders):
    st = load("airdrops.json", {"cursor": {}, "w": {}})
    latest = es.latest_block()
    default_start = es.block_by_time(time.time() - BACKFILL_DAYS * DAY)
    todo = sorted(holders)
    log(f"airdrops: scanning {len(todo)} holders")
    for i, a in enumerate(todo):
        start = st["cursor"].get(a, default_start)
        if start >= latest:
            continue
        own_hashes = {x["hash"] for x in es.account_paged("txlist", address=a, startblock=start, endblock=latest)
                      if x["from"].lower() == a}
        rows = []
        for action, kind in (("tokentx", "erc20"), ("tokennfttx", "erc721"), ("token1155tx", "erc1155")):
            for x in es.account_paged(action, address=a, startblock=start, endblock=latest):
                c = x["contractAddress"].lower()
                if c == CONTRACT:
                    continue
                if kind == "erc20":
                    try:
                        amt = int(x["value"]) / 10 ** int(x.get("tokenDecimal") or 0)
                    except ValueError:
                        amt = 0
                elif kind == "erc1155":
                    amt = int(x.get("tokenValue") or 1)
                else:
                    amt = 1
                rows.append((int(x["timeStamp"]), x["hash"], c, kind, x.get("tokenSymbol") or "",
                             x.get("tokenName") or "", x["from"].lower(), x["to"].lower(), amt))
        rows.sort()
        w = st["w"].setdefault(a, {})
        for ts, h, c, kind, sym, name, frm, to, amt in rows:
            if to == a and frm != a and h not in own_hashes:
                if any(s in (name + sym).lower() for s in SPAM_HINTS):
                    continue
                t = w.setdefault(c, {"kind": kind, "sym": sym[:20], "name": name[:40], "in": [], "out": []})
                t["in"].append([ts, amt])
            elif frm == a and c in w:
                w[c]["out"].append([ts, amt])
        if not w:
            st["w"].pop(a, None)
        st["cursor"][a] = latest + 1
        if i % 25 == 24:
            save("airdrops.json", st)
            log(f"  airdrops {i + 1}/{len(todo)} (calls {es.calls})")
    save("airdrops.json", st)

    # creation time for token contracts that hit >=5 holders
    reach = {}
    for a, toks in st["w"].items():
        for c in toks:
            reach[c] = reach.get(c, 0) + 1
    contracts = load("contracts.json", {})
    need = [c for c, n in reach.items() if n >= 5 and c not in contracts]
    for i in range(0, len(need), 5):
        batch = need[i:i + 5]
        res = es.contract_creation(batch) or []
        got = {r["contractAddress"].lower(): r for r in res}
        for c in batch:
            r = got.get(c)
            contracts[c] = {"created_ts": int(r["timestamp"]) if r and r.get("timestamp") else None,
                            "creator": (r or {}).get("contractCreator", "").lower() or None}
    save("contracts.json", contracts)
    log(f"airdrops: {len(st['w'])} holders with unsolicited tokens; {len(need)} contracts looked up")


def collect_stats(os_):
    try:
        d = os_.get(f"/collections/{SLUG}/stats")
        save("stats.json", d)
    except Exception as e:  # non-fatal
        log("stats failed:", e)


def main():
    steps = set(sys.argv[1:]) or {"transfers", "sales", "wallets", "airdrops", "stats"}
    es, os_ = Etherscan(), OpenSea()
    rows = collect_transfers(es) if "transfers" in steps else load_transfers()
    _, counts = holders_from(rows)
    if "stats" in steps:
        collect_stats(os_)
    if "sales" in steps:
        collect_sales(os_, es)
    if "wallets" in steps:
        collect_wallets(es, counts)
    if "airdrops" in steps:
        collect_airdrops(es, counts)
    log(f"done, etherscan calls: {es.calls}")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    main()
