"""start.py - Scheduled launcher for the up/down spread trading bot.

Runs the bot only between START_HOUR:START_MIN and STOP_HOUR:STOP_MIN
(system local time, 24h). Outside that window the bot is not running.

- Infinite loop: starts the bot when time >= start, stops it (and cleans up
  the process tree) when time >= stop.
- Ctrl+C: stops the bot, cleans up, and exits this launcher.
"""

import os
import sys
import time
import json
import signal
import subprocess
import threading

# ──────────────────────────────────────────────────────────────
# Schedule (system local time, 24-hour clock)
# ──────────────────────────────────────────────────────────────
START_HOUR = 0
START_MIN = 0
STOP_HOUR = 23
STOP_MIN = 59

# Bot launch command (mirrors how the bot is run manually)
BOT_ARGS = ["src/main.py", "--web", "--web-host", "127.0.0.1", "--web-port", "5050"]

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_proxy_from_config():
    path = os.path.join(PROJECT_ROOT, "config", "config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
        return cfg.get("proxy", "") or ""
    except Exception:
        return ""


_proxy = _load_proxy_from_config()
ENV_EXTRA = {}
if _proxy:
    ENV_EXTRA = {
        "HTTP_PROXY": _proxy,
        "HTTPS_PROXY": _proxy,
        "NO_PROXY": "127.0.0.1,localhost",
        "ALL_PROXY": _proxy,
        "all_proxy": _proxy,
    }
if sys.platform == "win32":
    VENV_PY = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
else:
    VENV_PY = os.path.join(PROJECT_ROOT, "venv", "bin", "python")

# Polling interval (seconds) for the time-check loop
CHECK_INTERVAL = 15

_bot_proc = None
_proc_lock = threading.Lock()
_running = True
_cooldown_until = 0.0  # timestamp; bot stays stopped until this time


def _now_minutes():
    """Current time as minutes-since-midnight (local)."""
    t = time.localtime()
    return t.tm_hour * 60 + t.tm_min


def _in_window():
    """True if current time is inside [start, stop).

    If stop time is earlier than start time, the window wraps past midnight
    (cross-day). E.g. start=18:00, stop=01:00 means: run from 18:00 today
    until 01:00 the next day (covers 18:00-23:59 and 00:00-00:59).
    """
    now = _now_minutes()
    start = START_HOUR * 60 + START_MIN
    stop = STOP_HOUR * 60 + STOP_MIN
    if start <= stop:
        # Same-day window (e.g. 09:00 -> 17:00)
        return start <= now < stop
    # Cross-day window (stop earlier than start, e.g. 18:00 -> 01:00)
    return now >= start or now < stop


def _check_consecutive_losses(n=6, max_age_sec=3600):
    """
    返回 True 当且仅当：
    1. 最近 n 笔交易（按时间排序）全是亏损；
    2. 最新的一笔交易发生时间距今不超过 max_age_sec 秒（默认 1 小时）。
    """
    coin_dirs = ["late_v3_btc", "late_v3_eth", "late_v3_sol"]
    trades = []                                   # (时间戳, 盈亏)
    now = time.time()

    for d in coin_dirs:
        path = os.path.join(PROJECT_ROOT, "logs", d, "trades.jsonl")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    pnl = rec.get("pnl")
                    if pnl is not None:
                        ts = rec.get("close_time", rec.get("entry_time", 0))
                        trades.append((ts, pnl))
                except json.JSONDecodeError:
                    continue

    if len(trades) < n:
        return False

    trades.sort(key=lambda x: x[0])               # 升序：最旧 -> 最新
    recent = trades[-n:]                          # 取最后 n 笔

    # 1) 全部亏损？
    if not all(pnl < 0 for _, pnl in recent):
        return False

    # 2) 最新一笔的时间是否在允许窗口内？
    latest_ts = recent[-1][0]                     # 最新交易的时间戳
    if (now - latest_ts) > max_age_sec:
        # 最新交易已经超过设定的时效，认为不需要再做冷却
        return False

    return True


def _kill_tree(proc):
    """Kill the bot process tree (venv launcher + uv child)."""
    if proc is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # noqa
        PROCESS_TERMINATE = 1
        # Enumerate child processes via taskkill (robust on Windows).
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    except Exception:
        # Fallback: terminate directly
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def start_bot():
    global _bot_proc
    with _proc_lock:
        if _bot_proc is not None and _bot_proc.poll() is None:
            return  # already running
        env = dict(os.environ)
        env.update(ENV_EXTRA)
        log_path = os.path.join(PROJECT_ROOT, "logs", "start_launcher.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        lf = open(log_path, "a", encoding="utf-8")
        _bot_proc = subprocess.Popen(
            [VENV_PY] + BOT_ARGS,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
        )
        print(f"[START] Bot launched (PID {_bot_proc.pid}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")


def stop_bot():
    global _bot_proc
    with _proc_lock:
        if _bot_proc is not None:
            print(f"[STOP] Stopping bot (PID {_bot_proc.pid}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            _kill_tree(_bot_proc)
            _bot_proc = None
            print("[STOP] Bot process tree cleaned up.")


def _signal_handler(signum, frame):
    global _running
    print("\n[START] Ctrl+C received - shutting down launcher.")
    _running = False
    stop_bot()


def main():
    global _running, _bot_proc, _cooldown_until
    signal.signal(signal.SIGINT, _signal_handler)
    sys.stdout.reconfigure(line_buffering=True)

    print(f"[START] Scheduler active. Window: {START_HOUR:02d}:{START_MIN:02d} -> "
          f"{STOP_HOUR:02d}:{STOP_MIN:02d} (local time). Ctrl+C to quit.")
    print("[START] Bot will auto-stop for 1h if 6 consecutive losing trades are detected.")

    bot_should_run = False
    while _running:
        try:
            # Cooldown active → wait
            if _cooldown_until > 0:
                remaining = _cooldown_until - time.time()
                if remaining > 0:
                    print(f"[START] Cooldown active - bot stops for {int(remaining // 60)}m {int(remaining % 60)}s")
                    time.sleep(min(CHECK_INTERVAL, remaining))
                    continue
                _cooldown_until = 0
                print("[START] Cooldown expired - resuming normal schedule.")

            in_window = _in_window()
            if in_window and not bot_should_run:
                start_bot()
                bot_should_run = True
            elif not in_window and bot_should_run:
                stop_bot()
                bot_should_run = False
            # If bot died unexpectedly while it should run, restart it.
            elif in_window and bot_should_run:
                with _proc_lock:
                    if _bot_proc is not None and _bot_proc.poll() is not None:
                        print("[START] Bot process exited unexpectedly - relaunching.")
                        _bot_proc = None
                        bot_should_run = False
                # Check consecutive losses
                if _check_consecutive_losses(6):
                    print(f"[START] 6 consecutive losses detected at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"- stopping bot for 1h.")
                    stop_bot()
                    bot_should_run = False
                    _cooldown_until = time.time() + 3600
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            _signal_handler(signal.SIGINT, None)
            break

    # Final cleanup on exit
    stop_bot()
    print("[START] Launcher exited.")


if __name__ == "__main__":
    main()