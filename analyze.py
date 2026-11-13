"""Turn collected data/*.json into docs/data.json (dashboard payload) and
append a daily snapshot to data/history.json."""
import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
DAY = 86400
ZERO = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
SERVICE_NONCE = 3000        # funders with more txs than this are treated as exchanges/services
AIRDROP_MIN_HOLDERS = 5     # token must reach this many holders to count as a collection airdrop
NEW_TOKEN_DAYS = 120        # contract created at most this long before its first airdrop
FLIP_DAYS = 7
ROUNDTRIP_DAYS = 30


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def week_of(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    monday = d.toordinal() - d.weekday()
    return datetime.fromordinal(monday).strftime("%Y-%m-%d")


def main():
    now = int(time.time())
    from collect import load_transfers
    transfers = load_transfers()
    sales = [s for s in load("sales.json", {"sales": []})["sales"] if s.get("price_eth") is not None]
    wallets = load("wallets.json", {})
    funders = load("funders.json", {})
    air = load("airdrops.json", {"w": {}})["w"]
    contracts = load("contracts.json", {})
    stats = load("stats.json", {})

    # ---------------------------------------------------------- holdings
    owner = {}
    holders_series = []
    counts = Counter()
    last_week = None
    for r in transfers:
        wk = week_of(r[1])
        if last_week and wk != last_week:
            holders_series.append([last_week, len(counts)])
        last_week = wk
        prev = owner.get(r[3])
        if prev and prev not in ZERO:
            counts[prev] -= 1
            if counts[prev] == 0:
                del counts[prev]
        owner[r[3]] = r[5]
        if r[5] not in ZERO:
            counts[r[5]] += 1
    if last_week:
        holders_series.append([last_week, len(counts)])
    holders = dict(counts)
    supply = sum(holders.values())

    # ---------------------------------------------------------- clusters
    def is_service(f):
        fi = funders.get(f) or {}
        return fi.get("nonce", 0) >= SERVICE_NONCE

    dsu = DSU()
    for a in holders:
        w = wallets.get(a) or {}
        f = w.get("funder")
        if w.get("is_contract") or not f or is_service(f):
            continue
        dsu.union(a, "F:" + f)
        if f in holders and not (wallets.get(f) or {}).get("is_contract"):
            dsu.union(a, f)
    groups = defaultdict(list)
    for a in holders:
        if a in dsu.p:
            groups[dsu.find(a)].append(a)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda a: -holders[a])
        fset = Counter((wallets.get(a) or {}).get("funder") for a in members)
        clusters.append({
            "members": members, "size": len(members),
            "sprotos": sum(holders[a] for a in members),
            "funders": [f for f, _ in fset.most_common(3)],
            "exactly3": sum(1 for a in members if holders[a] == 3),
        })
    clusters.sort(key=lambda c: (-c["sprotos"], -c["size"]))
    cluster_of = {}
    for i, c in enumerate(clusters):
        c["id"] = f"C{i + 1}"
        for a in c["members"]:
            cluster_of[a] = c["id"]

    def entity(a):
        return cluster_of.get(a, a)

    # ---------------------------------------------------------- sales flags
    wflags = defaultdict(set)
    sale_flags = []
    by_token = defaultdict(list)
    for s in sales:
        by_token[s["token"]].append(s)
    flips = roundtrips = intra = 0
    for tok, ss in by_token.items():
        ss.sort(key=lambda s: s["ts"])
        for i, s in enumerate(ss):
            for p in reversed(ss[:i]):
                if p["buyer"] == s["seller"]:
                    if s["ts"] - p["ts"] <= FLIP_DAYS * DAY:
                        s.setdefault("flags", []).append("flip7d")
                        wflags[s["seller"]].add("flip7d")
                        flips += 1
                    break
            for p in reversed(ss[:i]):
                if s["ts"] - p["ts"] > ROUNDTRIP_DAYS * DAY:
                    break
                if p["seller"] == s["buyer"]:
                    s.setdefault("flags", []).append("roundtrip")
                    wflags[s["buyer"]].add("roundtrip")
                    wflags[s["seller"]].add("roundtrip")
                    roundtrips += 1
                    break
    pair_sales = defaultdict(list)
    for s in sales:
        pair_sales[(s["seller"], s["buyer"])].append(s["ts"])
    wash_pairs = []
    for (a, b), tss in pair_sales.items():
        if a < b and (b, a) in pair_sales:
            back = pair_sales[(b, a)]
            if min(abs(x - y) for x in tss for y in back) <= ROUNDTRIP_DAYS * DAY:
                wash_pairs.append({"a": a, "b": b, "a_to_b": len(tss), "b_to_a": len(back)})
                wflags[a].add("pair-wash")
                wflags[b].add("pair-wash")
    for s in sales:
        cs, cb = cluster_of.get(s["seller"]), cluster_of.get(s["buyer"])
        if cs and cs == cb:
            s.setdefault("flags", []).append("intra-cluster")
            wflags[s["seller"]].add("intra-cluster")
            wflags[s["buyer"]].add("intra-cluster")
            intra += 1
        if s.get("flags"):
            sale_flags.append(s)

    # ---------------------------------------------------------- weekly
    weekly = defaultdict(lambda: {"sales": 0, "buyers": set(), "sellers": set(), "vol": 0.0})
    for s in sales:
        w = weekly[week_of(s["ts"])]
        w["sales"] += 1
        w["buyers"].add(s["buyer"])
        w["sellers"].add(s["seller"])
        w["vol"] += s["price_eth"]
    weekly_rows = [{"week": k, "sales": v["sales"], "unique_buyers": len(v["buyers"]),
                    "unique_sellers": len(v["sellers"]), "volume": round(v["vol"], 4)}
                   for k, v in sorted(weekly.items())]

    # ---------------------------------------------------------- shares
    def top_shares(window_days):
        cut = now - window_days * DAY
        ss = [s for s in sales if s["ts"] >= cut]
        total = sum(s["price_eth"] for s in ss) or 0
        vol = Counter()
        for s in ss:
            for e in {entity(s["buyer"]), entity(s["seller"])}:
                vol[e] += s["price_eth"]
        top = [{"entity": e, "volume": round(v, 4), "share": round(v / total, 4) if total else 0}
               for e, v in vol.most_common(5)]
        return total, len(ss), top

    sup = Counter()
    for a, n in holders.items():
        sup[entity(a)] += n
    top_supply = [{"entity": e, "sprotos": n, "share": round(n / supply, 4)} for e, n in sup.most_common(5)]
    vol90, n90, top_vol90 = top_shares(90)
    vol30, n30, top_vol30 = top_shares(30)
    buyers30 = len({s["buyer"] for s in sales if s["ts"] >= now - 30 * DAY})

    # ---------------------------------------------------------- airdrops
    first_seen = {}
    reach = Counter()
    for a, toks in air.items():
        for c, t in toks.items():
            if t["in"]:
                reach[c] += 1
                first_seen[c] = min(first_seen.get(c, 1 << 62), t["in"][0][0])
    qualifying = {}
    for c, n in reach.items():
        ci = contracts.get(c) or {}
        ct = ci.get("created_ts")
        if n >= AIRDROP_MIN_HOLDERS and ct and 0 <= first_seen[c] - ct <= NEW_TOKEN_DAYS * DAY:
            qualifying[c] = ct
    tok_stats = {}
    dump_share = {}
    for a, toks in air.items():
        shares = []
        for c, t in toks.items():
            if c not in qualifying or not t["in"]:
                continue
            got = sum(x[1] for x in t["in"]) or 0
            t0 = t["in"][0][0]
            out24 = sum(x[1] for x in t["out"] if t0 <= x[0] <= t0 + DAY)
            outs = [x[0] for x in t["out"] if x[0] >= t0]
            frac = min(out24 / got, 1.0) if got else 0
            shares.append(frac)
            ts_ = tok_stats.setdefault(c, {"contract": c, "symbol": t["sym"], "name": t["name"], "kind": t["kind"],
                                           "created_ts": qualifying[c], "first_drop_ts": first_seen[c],
                                           "recipients": 0, "sold_any": 0, "dumped_24h": 0, "hours": [],
                                           "held_by_recipients": 0})
            ts_["recipients"] += 1
            if outs:
                ts_["sold_any"] += 1
                ts_["hours"].append((outs[0] - t0) / 3600)
            if frac > 0.5:
                ts_["dumped_24h"] += 1
        if shares:
            dump_share[a] = sum(shares) / len(shares)
    for t in tok_stats.values():
        t["median_hours_to_sell"] = round(statistics.median(t["hours"]), 1) if t["hours"] else None
        del t["hours"]
        del t["held_by_recipients"]
    airdrop_tokens = sorted(tok_stats.values(), key=lambda t: -t["recipients"])
    dumpers = {a for a, v in dump_share.items() if v > 0.5}
    for a in dumpers:
        wflags[a].add("airdrop-dumper")

    # ---------------------------------------------------------- wallet table
    act = defaultdict(lambda: {"buys": 0, "sells": 0, "vol": 0.0})
    for s in sales:
        if s["ts"] >= now - 90 * DAY:
            act[s["buyer"]]["buys"] += 1
            act[s["seller"]]["sells"] += 1
            act[s["buyer"]]["vol"] += s["price_eth"]
            act[s["seller"]]["vol"] += s["price_eth"]
    csize = {c["id"]: c["size"] for c in clusters}
    table = []
    for a, n in holders.items():
        w = wallets.get(a) or {}
        f = w.get("funder")
        flags = set(wflags.get(a, ()))
        if n == 3 and a in cluster_of:
            flags.add("3-in-cluster")
        if w.get("is_contract"):
            flags.add("contract")
        table.append({
            "address": a, "held": n, "cluster": cluster_of.get(a), "cluster_size": csize.get(cluster_of.get(a)),
            "age_days": round((now - w["first_ts"]) / DAY) if w.get("first_ts") else None,
            "funder": f, "funder_service": bool(f and is_service(f)),
            "buys90": act[a]["buys"], "sells90": act[a]["sells"], "vol90": round(act[a]["vol"], 3),
            "dump_share": round(dump_share[a], 2) if a in dump_share else None,
            "flags": sorted(flags),
        })
    table.sort(key=lambda r: -r["held"])

    in_clusters = sum(c["sprotos"] for c in clusters)
    ex3 = sum(c["exactly3"] for c in clusters)
    total = stats.get("total", {})
    summary = {
        "updated": now, "holders": len(holders), "supply_held": supply,
        "floor": total.get("floor_price"), "floor_symbol": total.get("floor_price_symbol", "ETH"),
        "sales_30d": n30, "volume_30d": round(vol30, 3), "unique_buyers_30d": buyers30,
        "sales_90d": n90, "volume_90d": round(vol90, 3),
        "clusters": len(clusters), "clustered_wallets": sum(c["size"] for c in clusters),
        "clustered_sprotos": in_clusters, "clustered_share": round(in_clusters / supply, 4) if supply else 0,
        "exactly3_in_clusters": ex3, "exactly3_total": sum(1 for n in holders.values() if n == 3),
        "flips_7d": flips, "roundtrips": roundtrips, "wash_pairs": len(wash_pairs), "intra_cluster_sales": intra,
        "top5_supply_share": round(sum(x["share"] for x in top_supply), 4),
        "top5_volume_share_90d": round(sum(x["share"] for x in top_vol90), 4),
        "airdrop_tokens": len(airdrop_tokens), "airdrop_recipients": len(dump_share),
        "airdrop_dumpers": len(dumpers),
        "wallets_with_funding": sum(1 for a in holders if (wallets.get(a) or {}).get("funder")),
    }

    # ---------------------------------------------------------- history
    hist = load("history.json", [])
    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    snap = {"date": today, **{k: summary[k] for k in (
        "holders", "floor", "clusters", "clustered_share", "exactly3_in_clusters", "top5_supply_share",
        "top5_volume_share_90d", "airdrop_dumpers", "sales_30d", "unique_buyers_30d", "volume_30d")}}
    hist = [h for h in hist if h["date"] != today] + [snap]
    (DATA / "history.json").write_text(json.dumps(hist, indent=1))

    recent_sales = sorted(sales, key=lambda s: -s["ts"])[:300]
    out = {
        "summary": summary, "history": hist, "weekly": weekly_rows, "holders_series": holders_series,
        "wallets": table,
        "clusters": [{k: c[k] for k in ("id", "size", "sprotos", "exactly3", "funders", "members")} for c in clusters],
        "top_supply": top_supply, "top_volume_90d": top_vol90, "top_volume_30d": top_vol30,
        "wash_pairs": wash_pairs,
        "flagged_sales": [{k: s.get(k) for k in ("ts", "token", "seller", "buyer", "price_eth", "market", "flags", "tx")}
                          for s in sorted(sale_flags, key=lambda s: -s["ts"])],
        "recent_sales": [{k: s.get(k) for k in ("ts", "token", "seller", "buyer", "price_eth", "market", "tx")}
                         for s in recent_sales],
        "airdrop_tokens": airdrop_tokens,
        "markets": Counter(s.get("market", "?").split(":")[0] for s in sales if s["ts"] >= now - 90 * DAY),
        "params": {"service_nonce": SERVICE_NONCE, "airdrop_min_holders": AIRDROP_MIN_HOLDERS,
                   "new_token_days": NEW_TOKEN_DAYS, "flip_days": FLIP_DAYS, "roundtrip_days": ROUNDTRIP_DAYS},
    }
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(json.dumps(out, separators=(",", ":")))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
