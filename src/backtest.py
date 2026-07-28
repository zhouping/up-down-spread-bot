"""
Offline backtester for Meridian (late_entry_v3).

Replays the entry logic of the strategy against recorded orderbook history
(logs/book_history/{coin}/{slug}.jsonl, produced by data_feed._record_book) and
uses the REAL settlement outcome (fetched from the Polymarket Gamma API, with a
fallback to logs/late_v3_*/trades.jsonl) to compute PnL.

Usage:
  python backtest.py                       # backtest with config params (方案A)
  python backtest.py --min-confidence 0.5 --price-max 0.85
  python backtest.py --grid                # sweep entry parameters
  python backtest.py --coins btc,eth --no-proxy
"""
import argparse
import json
import glob
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"
GAMMA_API = "https://gamma-api.polymarket.com"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_book_history(base: Path):
    """slug -> list of book records (sorted by time)."""
    data = defaultdict(list)
    if not base.is_dir():
        return data
    for coin_dir in sorted(base.iterdir()):
        if not coin_dir.is_dir():
            continue
        for fp in sorted(coin_dir.glob("*.jsonl")):
            for line in open(fp, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    data[rec["slug"]].append(rec)
                except Exception:
                    pass
    for slug in data:
        data[slug].sort(key=lambda r: r.get("t", 0))
    return data


def load_trades_winners():
    """slug -> winner from existing trade ledger (fallback ground truth)."""
    out = {}
    for fp in glob.glob(str(ROOT / "logs" / "late_v3_*" / "trades.jsonl")):
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
                out[t["market_slug"]] = t.get("winner")
            except Exception:
                pass
    return out


def get_winner_from_gamma(slug, timeout=15):
    try:
        url = f"{GAMMA_API}/events?slug={slug}"
        r = requests_get(url, timeout=timeout)
        if r is None or r.status_code != 200:
            return None
        evs = r.json()
        if not evs:
            return None
        m = evs[0]["markets"][0]
        outcomes = m.get("outcomes") or []
        prices = m.get("outcomePrices") or []
        if len(outcomes) != len(prices):
            return None
        for o, p in zip(outcomes, prices):
            try:
                if float(p) == 1.0:
                    ol = str(o).lower()
                    if ol == "up":
                        return "UP"
                    if ol == "down":
                        return "DOWN"
            except Exception:
                pass
        return None
    except Exception:
        return None


def parse_slug(slug):
    """Return (coin, interval_sec, market_end_unix) or None."""
    parts = slug.split("-")
    # expected: coin, updown, 15m|5m, slot
    if len(parts) < 4:
        return None
    coin = parts[0]
    window = parts[2]
    try:
        slot = int(parts[3])
    except Exception:
        return None
    if window == "15m":
        interval = 900
    elif window == "5m":
        interval = 300
    else:
        try:
            interval = int(window.replace("m", "")) * 60
        except Exception:
            return None
    return coin, interval, slot + interval


def size_for_tier(sec_left, sizing):
    if sec_left > 180:
        return int(sizing.get("above_180_sec", 8))
    if sec_left > 120:
        return int(sizing.get("above_120_sec", 10))
    return int(sizing.get("below_120_sec", 12))


def replay_market(records, params, winner, sizing):
    """Replay one market's book history with given entry params.
    Returns a trade dict, or None if not traded / no ground truth."""
    if not records or winner not in ("UP", "DOWN"):
        return None
    parsed = parse_slug(records[0]["slug"])
    if not parsed:
        return None
    _, _, market_end = parsed

    entered = False
    side = None
    invested = 0.0
    shares = 0.0
    entries = 0
    last_entry_t = -1e9

    for rec in records:
        t = rec.get("t", 0)
        sec_left = market_end - t
        if sec_left <= 0 or sec_left > params["entry_window_sec"]:
            continue
        up = rec.get("up_ask")
        down = rec.get("down_ask")
        if not up or not down:
            continue
        spread = up + down
        if spread <= 0 or spread > params["max_spread"]:
            continue
        conf = abs(up - down)
        if conf < params["min_confidence"]:
            continue
        fav_side = "UP" if up > down else "DOWN"
        fav_price = max(up, down)
        if fav_price > params["price_max"]:
            continue
        if not entered:
            side = fav_side
            entered = True
        elif fav_side != side:
            continue  # single-side only (no flip handling in backtest)
        if t - last_entry_t < params["entry_frequency_sec"]:
            continue
        size = size_for_tier(sec_left, sizing)
        cost = size * fav_price
        if invested + cost > params["max_investment_per_market"]:
            room = params["max_investment_per_market"] - invested
            if room <= 0:
                break
            size = int(room / fav_price)
            if size <= 0:
                break
            cost = size * fav_price
        invested += cost
        shares += size
        entries += 1
        last_entry_t = t

    if shares <= 0:
        return None
    pnl = (shares * 1.0 - invested) if side == winner else -invested
    return {
        "slug": records[0]["slug"],
        "coin": parsed[0],
        "side": side,
        "winner": winner,
        "entries": entries,
        "invested": round(invested, 2),
        "shares": round(shares, 2),
        "avg_entry": round(invested / shares, 4) if shares else 0,
        "pnl": round(pnl, 2),
        "roi": round(pnl / invested * 100, 2) if invested else 0,
    }


def aggregate(trades):
    agg = defaultdict(lambda: {
        "markets": 0, "wins": 0, "losses": 0,
        "invested": 0.0, "pnl": 0.0, "entries": 0,
    })
    for tr in trades:
        c = tr["coin"]
        a = agg[c]
        a["markets"] += 1
        a["entries"] += tr["entries"]
        a["invested"] += tr["invested"]
        a["pnl"] += tr["pnl"]
        if tr["pnl"] > 0:
            a["wins"] += 1
        else:
            a["losses"] += 1
    return agg


def report(agg, label=""):
    total_m = sum(a["markets"] for a in agg.values())
    total_inv = sum(a["invested"] for a in agg.values())
    total_pnl = sum(a["pnl"] for a in agg.values())
    total_w = sum(a["wins"] for a in agg.values())
    roi = total_pnl / total_inv * 100 if total_inv else 0
    wr = total_w / total_m * 100 if total_m else 0
    print(f"\n=== Backtest {label} ===")
    print(f"{'coin':5} {'mkts':>4} {'win/loss':>8} {'WR%':>6} {'invested':>9} {'pnl':>9} {'ROI%':>7}")
    for c in sorted(agg):
        a = agg[c]
        wr = a["wins"] / a["markets"] * 100 if a["markets"] else 0
        roi = a["pnl"] / a["invested"] * 100 if a["invested"] else 0
        print(f"{c:5} {a['markets']:>4} {a['wins']}/{a['losses']:>6} {wr:>6.1f} "
              f"{a['invested']:>9.2f} {a['pnl']:>+9.2f} {roi:>7.1f}")
    print(f"{'ALL':5} {total_m:>4} {total_w}/{total_m - total_w:>6} {wr:>6.1f} "
          f"{total_inv:>9.2f} {total_pnl:>+9.2f} {roi:>7.1f}")
    return {"markets": total_m, "pnl": total_pnl, "roi": roi, "wr": wr}


# Lazy import of requests so the script can run without network for pure replay
_requests = None
_proxies = None


def requests_get(url, timeout=15):
    global _requests, _proxies
    if _requests is None:
        import requests as _r
        _requests = _r
        import os
        px = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
        _proxies = {"http": px, "https": px} if px else None
    return _requests.get(url, timeout=timeout, proxies=_proxies)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default=None, help="comma list, e.g. btc,eth")
    ap.add_argument("--entry-window", type=int, default=None)
    ap.add_argument("--min-confidence", type=float, default=None)
    ap.add_argument("--price-max", type=float, default=None)
    ap.add_argument("--max-spread", type=float, default=None)
    ap.add_argument("--entry-frequency", type=int, default=None)
    ap.add_argument("--grid", action="store_true", help="sweep entry parameters")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--book-dir", default=None)
    args = ap.parse_args()

    if args.no_proxy:
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("https_proxy", None)
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("http_proxy", None)
        global _proxies
        _proxies = None

    cfg = load_config()
    strat = cfg.get("strategy", {})
    sizing = strat.get("sizing", {})
    params = {
        "entry_window_sec": args.entry_window or int(strat.get("entry_window_sec", 240)),
        "entry_frequency_sec": args.entry_frequency or int(strat.get("entry_frequency_sec", 7)),
        "min_confidence": args.min_confidence if args.min_confidence is not None else float(strat.get("min_confidence", 0.30)),
        "max_spread": args.max_spread if args.max_spread is not None else float(strat.get("max_spread", 1.05)),
        "price_max": args.price_max if args.price_max is not None else float(strat.get("price_max", 0.92)),
        "max_investment_per_market": float(strat.get("max_investment_per_market", 300)),
    }

    book_base = Path(args.book_dir) if args.book_dir else (ROOT / cfg.get("data_sources", {}).get("polymarket", {}).get("book_history_dir", "logs/book_history"))
    book = load_book_history(book_base)
    print(f"[BACKTEST] book history markets loaded: {len(book)} from {book_base}")
    if not book:
        print("[BACKTEST] No book history found. Run the bot with record_book_history=true to collect data first.")
        return

    winners_gamma = {}
    trades_winners = load_trades_winners()
    coin_filter = set(c.strip().lower() for c in (args.coins or "").split(",") if c.strip()) or None

    def resolve_winner(slug):
        if slug in trades_winners and trades_winners[slug] in ("UP", "DOWN"):
            return trades_winners[slug]
        if slug not in winners_gamma:
            winners_gamma[slug] = get_winner_from_gamma(slug)
        return winners_gamma[slug]

    def run(params):
        trades = []
        for slug, recs in book.items():
            if coin_filter and parse_slug(slug) and parse_slug(slug)[0] not in coin_filter:
                continue
            w = resolve_winner(slug)
            tr = replay_market(recs, params, w, sizing)
            if tr:
                trades.append(tr)
        return trades

    if args.grid:
        confs = [0.30, 0.40, 0.50]
        pmaxs = [0.85, 0.88, 0.92]
        wins = [120, 180, 240]
        print("\nGRID (min_conf / price_max / entry_window) -> markets, WR%, ROI%")
        for cf in confs:
            for pm_ in pmaxs:
                row = []
                for ew in wins:
                    p = dict(params, min_confidence=cf, price_max=pm_, entry_window_sec=ew)
                    tr = run(p)
                    agg = aggregate(tr)
                    tm = sum(a["markets"] for a in agg.values())
                    tp = sum(a["pnl"] for a in agg.values())
                    ti = sum(a["invested"] for a in agg.values())
                    tw = sum(a["wins"] for a in agg.values())
                    wr = tw / tm * 100 if tm else 0
                    roi = tp / ti * 100 if ti else 0
                    row.append(f"w{ew}:{tm}m/{wr:.0f}%/{roi:.1f}%")
                print(f"  conf={cf} pmax={pm_} | " + "  ".join(row))
        return

    trades = run(params)
    agg = aggregate(trades)
    report(agg, label=f"(window={params['entry_window_sec']} conf={params['min_confidence']} "
                     f"pmax={params['price_max']} spread<={params['max_spread']})")
    print(f"\n[DEBUG] total trades replayed: {len(trades)}")


if __name__ == "__main__":
    main()
