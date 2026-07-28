"""
Build JSON-serializable dashboard snapshot from live trading objects.
"""
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

_CST = timezone(timedelta(hours=8))


def _entry_timestamp_of(t: Dict[str, Any]) -> str:
    """Return entry time string (CST +8), backfilling from entry_time/unix if needed."""
    ts = t.get("entry_timestamp") or ""
    if ts:
        return ts
    et = t.get("entry_time") or t.get("start_time") or 0
    if et:
        try:
            return datetime.fromtimestamp(float(et), tz=_CST).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, TypeError):
            return ""
    return ""


def build_snapshot(
    *,
    coins: List[str],
    strategy_base: str,
    multi_trader,
    data_feed,
    wallet_balance: Optional[float],
    config: Dict[str, Any],
    session_start_time: float,
    dry_run: bool,
    markets_skipped: Dict[str, int],
    initial_balance: Optional[float] = None,
    open_investment: float = 0.0,
) -> Dict[str, Any]:
    now = time.time()
    uptime = now - session_start_time

    portfolio = multi_trader.get_portfolio_stats()

    # All strategies share ONE wallet balance (capital_per_strategy=0), so the
    # per-trader current_capital/starting_capital are meaningless. Compute the
    # realized PnL / ROI from actual closed trades instead (not wallet delta,
    # which would wrongly count open-position capital as a loss).
    _realized_pnl = sum(
        (t.get_performance_stats().get("total_pnl", 0) or 0)
        for t in getattr(multi_trader, "traders", {}).values()
    )
    _initial = initial_balance if initial_balance else 0.0
    _portfolio_roi = (_realized_pnl / _initial * 100) if _initial > 0 else 0.0
    portfolio["total_pnl"] = round(_realized_pnl, 2)
    portfolio["portfolio_roi"] = round(_portfolio_roi, 2)

    # "剩余资金" (remaining funds) = initial + realized PnL - currently invested
    # in OPEN positions. Recomputed live from authoritative state every cycle so
    # it can never drift from the real wallet (no reliance on callback updates).
    _remaining = (_initial + _realized_pnl) - open_investment
    wallet_balance = round(_remaining, 2)

    coin_blocks: Dict[str, Any] = {}
    for coin in coins:
        trader_name = f"{strategy_base}_{coin}"
        st = data_feed.get_state(coin)
        trader = multi_trader.traders.get(trader_name)

        ms: Dict[str, Any] = {
            "market_slug": st.get("market_slug") or "",
            "seconds_till_end": int(st.get("seconds_till_end") or 0),
            "up_ask": float(st.get("up_ask") or 0),
            "down_ask": float(st.get("down_ask") or 0),
            "confidence": float(st.get("confidence") or 0),
            "price": float(st.get("price") or 0),
        }
        ua, da = ms["up_ask"], ms["down_ask"]
        ms["favorite"] = "UP" if ua > da else "DOWN"

        trading_cfg = config.get("trading", {}).get(coin, {})
        ms["trading_enabled"] = bool(trading_cfg.get("enabled", True))
        ms["trading_reason"] = trading_cfg.get("reason") or ""

        pos_detail = None
        if trader:
            perf = trader.get_performance_stats()
            pnl_coin = trader.current_capital - trader.starting_capital
            slug = ms["market_slug"]
            ms["stats"] = {
                "pnl": round(pnl_coin, 2),
                "total_trades": perf.get("total_trades", 0),
                "wins": perf.get("wins", 0),
                "losses": perf.get("losses", 0),
                "win_rate": round(perf.get("win_rate", 0), 2),
            }
            if slug:
                pos = multi_trader.get_current_positions(trader_name, slug)
                if pos and (pos.get("up_shares", 0) > 0 or pos.get("down_shares", 0) > 0):
                    detailed = trader.get_market_detailed_stats(slug, ua, da)
                    if detailed:
                        pos_detail = {
                            "up_shares": detailed.get("up_shares", 0),
                            "down_shares": detailed.get("down_shares", 0),
                            "up_invested": round(detailed.get("up_invested", 0), 2),
                            "down_invested": round(detailed.get("down_invested", 0), 2),
                            "total_invested": round(detailed.get("total_invested", 0), 2),
                            "unrealized_pnl": round(detailed.get("unrealized_pnl", 0), 2),
                            "unrealized_pct": round(detailed.get("unrealized_pct", 0), 2),
                            "max_drawdown": round(detailed.get("max_drawdown", 0), 2),
                            "entries_count": detailed.get("entries_count", 0),
                            "our_side": "UP"
                            if detailed.get("up_shares", 0) > detailed.get("down_shares", 0)
                            else "DOWN",
                        }
                        pos_detail["if_up_wins"] = round(
                            (pos_detail["up_shares"] * 1.0) - pos_detail["total_invested"], 2
                        )
                        pos_detail["if_down_wins"] = round(
                            (pos_detail["down_shares"] * 1.0) - pos_detail["total_invested"], 2
                        )
        else:
            ms["stats"] = None

        ms["position"] = pos_detail
        coin_blocks[coin] = ms

    recent: List[Dict[str, Any]] = []
    for name, tr in multi_trader.traders.items():
        closed = getattr(tr, "closed_trades", []) or []
        for trade in closed:
            t = dict(trade)
            t["strategy"] = name
            recent.append(t)
    recent.sort(key=lambda x: x.get("close_time", 0), reverse=True)
    recent_trimmed = []
    for t in recent[:500]:
        close_time = t.get("close_time") or 0
        dt = (
            datetime.fromtimestamp(close_time).strftime("%Y%m%d_%H%M%S")
            if close_time
            else "------------"
        )
        up_shares = float(t.get("up_shares") or 0)
        down_shares = float(t.get("down_shares") or 0)
        total_shares = up_shares + down_shares
        amount = float(t.get("total_cost") or 0)
        if not amount:
            amount = float(t.get("up_invested") or 0) + float(t.get("down_invested") or 0)
        entry_price = round(amount / total_shares, 4) if total_shares > 0 else 0.0
        recent_trimmed.append(
            {
                "market_slug": t.get("market_slug"),
                "entry_timestamp": _entry_timestamp_of(t),
                "close_time": close_time,
                "datetime": dt,
                "winner": t.get("winner"),
                "entry_price": entry_price,
                "contracts": round(total_shares, 2),
                "amount": round(amount, 2),
                "pnl": round(float(t.get("pnl", 0)), 2),
                "fee": round(float(t.get("total_fee", 0) or 0), 4),
            }
        )

    strat_cfg = config.get("strategy", {})
    safety_cfg = config.get("safety", {})
    exit_cfg = config.get("exit", {})
    pm = config.get("data_sources", {}).get("polymarket", {})
    market_interval_sec = int(pm.get("market_interval_sec", 900))

    return {
        "status": "running",
        "uptime_sec": round(uptime, 1),
        "session_start": session_start_time,
        "wallet_balance": round(wallet_balance, 2) if wallet_balance is not None else None,
        "dry_run": dry_run,
        "markets_skipped": dict(markets_skipped),
        "portfolio": {
            "total_capital": round(portfolio.get("total_capital", 0), 2),
            "total_pnl": round(portfolio.get("total_pnl", 0), 2),
            "portfolio_roi": round(portfolio.get("portfolio_roi", 0), 2),
            "total_trades": portfolio.get("total_trades", 0),
        },
        "market_interval_sec": market_interval_sec,
        "market_label": "5m" if market_interval_sec == 300 else ("15m" if market_interval_sec == 900 else f"{market_interval_sec}s"),
        "strategy_summary": {
            "entry_window_sec": strat_cfg.get("entry_window_sec"),
            "entry_frequency_sec": strat_cfg.get("entry_frequency_sec"),
            "min_confidence": strat_cfg.get("min_confidence"),
            "price_max": strat_cfg.get("price_max"),
            "max_spread": strat_cfg.get("max_spread"),
            "max_investment_per_market": strat_cfg.get("max_investment_per_market"),
            "sizing": strat_cfg.get("sizing", {}),
        },
        "safety_summary": {
            "max_order_size_usd": safety_cfg.get("max_order_size_usd"),
            "max_orders_per_minute": safety_cfg.get("max_orders_per_minute"),
            "max_total_investment": safety_cfg.get("max_total_investment"),
        },
        "flip_stop": exit_cfg.get("flip_stop", {}),
        "coins": coin_blocks,
        "recent_trades": recent_trimmed,
    }
