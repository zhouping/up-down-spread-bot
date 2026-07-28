"""
Thread-safe snapshot + stop request for the web dashboard (same process as the bot).
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_lock = threading.RLock()
_snapshot: Dict[str, Any] = {"status": "initializing"}
_stop_requested = False
_trading_paused = False
_session_start: float = 0.0
_trade_logging_paused = False
_multi_trader = None

# ── Wallet / capital tracking (single source of truth) ──
_wallet_balance = 0.0
_initial_balance = 100.0
_total_invested_open = 0.0


def set_multi_trader(mt) -> None:
    global _multi_trader
    with _lock:
        _multi_trader = mt


def get_multi_trader():
    with _lock:
        return _multi_trader


def set_wallet_balance(value: float) -> None:
    global _wallet_balance
    with _lock:
        _wallet_balance = value


def get_wallet_balance() -> float:
    with _lock:
        return _wallet_balance


def set_initial_balance(value: float) -> None:
    global _initial_balance
    with _lock:
        _initial_balance = value


def get_initial_balance() -> float:
    with _lock:
        return _initial_balance


def add_open_investment(amount: float) -> None:
    global _total_invested_open
    with _lock:
        _total_invested_open += amount


def get_open_investment() -> float:
    with _lock:
        return _total_invested_open


def reset_capital_and_open() -> None:
    """Used by 'clear records': restore wallet to initial, drop open total."""
    global _wallet_balance, _total_invested_open
    with _lock:
        _wallet_balance = _initial_balance
        _total_invested_open = 0.0


def apply_close_to_balance(pnl: float, cost: float) -> None:
    """Roll wallet balance on close.

    Bookkeeping model: open trades move cash into `_total_invested_open` (the
    entry side does NOT decrement `_wallet_balance`); on close we credit the
    full payout (= pnl + cost) back to `_wallet_balance` and release the cost
    from open investment. This keeps `_wallet_balance` == initial + realized PnL.
    """
    global _wallet_balance, _total_invested_open
    with _lock:
        _wallet_balance += pnl + cost
        _total_invested_open = max(0.0, _total_invested_open - cost)


def pause_trade_logging() -> None:
    global _trade_logging_paused
    with _lock:
        _trade_logging_paused = True


def resume_trade_logging() -> None:
    global _trade_logging_paused
    with _lock:
        _trade_logging_paused = False


def is_trade_logging_paused() -> bool:
    with _lock:
        return _trade_logging_paused


def set_session_start(ts: float) -> None:
    global _session_start
    with _lock:
        _session_start = ts


def set_snapshot(data: Dict[str, Any]) -> None:
    """Called from main trading loop (every ~0.1s)."""
    global _snapshot
    with _lock:
        data = dict(data)
        data["updated_at"] = time.time()
        _snapshot = data


def get_snapshot() -> Dict[str, Any]:
    with _lock:
        return dict(_snapshot)


def request_stop() -> None:
    global _stop_requested
    with _lock:
        _stop_requested = True


def consume_stop_request() -> bool:
    """Main loop: if True, set stop_flag and clear request."""
    global _stop_requested
    with _lock:
        if _stop_requested:
            _stop_requested = False
            return True
        return False


def set_trading_paused(paused: bool) -> None:
    global _trading_paused
    with _lock:
        _trading_paused = paused


def is_trading_paused() -> bool:
    with _lock:
        return _trading_paused


def write_state_file(project_root: Path, data: Dict[str, Any]) -> None:
    """Optional: write logs/bot_state.json for read-only monitoring without shared memory."""
    path = project_root / "logs" / "bot_state.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        payload = dict(data)
        payload["updated_at"] = time.time()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(path)
    except OSError:
        pass


def read_state_file(project_root: Path) -> Optional[Dict[str, Any]]:
    path = project_root / "logs" / "bot_state.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
