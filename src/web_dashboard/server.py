"""
Flask web dashboard: API + static UI.
Run inside the bot process (--web) or standalone (reads logs/bot_state.json).
"""
import json
import shutil
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from market_config import apply_market_window_settings

# Project root: repository root (parent of /config, /src)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def create_app(project_root: Path | None = None) -> Flask:
    root = project_root or PROJECT_ROOT

    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        template_folder=str(TEMPLATE_DIR),
    )

    @app.route("/")
    def index():
        from flask import render_template

        return render_template("index.html")

    @app.route("/api/health")
    def health():
        import web_dashboard_state as wds

        snap = wds.get_snapshot()
        ts = snap.get("updated_at", 0)
        age = time.time() - ts if ts else 9999
        file_snap = wds.read_state_file(root)
        file_ts = file_snap.get("updated_at", 0) if file_snap else 0
        file_age = time.time() - file_ts if file_ts else 9999
        bot_live = age < 15.0 or file_age < 15.0
        import web_dashboard_state as wds

        return jsonify(
            {
                "ok": True,
                "bot_live": bot_live,
                "snapshot_age_sec": round(min(age, file_age), 2),
                "trading_paused": wds.is_trading_paused(),
            }
        )

    @app.route("/api/status")
    def api_status():
        import web_dashboard_state as wds

        snap = wds.get_snapshot()
        snap["trading_paused"] = wds.is_trading_paused()
        if snap.get("status") == "initializing" or not snap.get("coins"):
            file_snap = wds.read_state_file(root)
            if file_snap:
                file_snap["trading_paused"] = wds.is_trading_paused()
                return jsonify(file_snap)
        return jsonify(snap)

    @app.route("/api/config", methods=["GET"])
    def get_config():
        if not CONFIG_PATH.exists():
            return jsonify({"error": "config.json not found"}), 404
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            apply_market_window_settings(data)
            return jsonify(data)
        except (OSError, json.JSONDecodeError) as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config", methods=["POST"])
    def post_config():
        if not request.is_json:
            return jsonify({"error": "Expected JSON body"}), 400
        body = request.get_json()
        if not isinstance(body, dict):
            return jsonify({"error": "Invalid JSON"}), 400
        apply_market_window_settings(body)
        if not CONFIG_PATH.parent.is_dir():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        backup = CONFIG_PATH.with_suffix(".json.bak")
        try:
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, backup)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=2)
            return jsonify({"ok": True, "message": "Saved. Restart the bot to apply."})
        except OSError as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bot/pause", methods=["POST"])
    def bot_pause():
        import web_dashboard_state as wds

        wds.set_trading_paused(True)
        return jsonify({"ok": True, "paused": True, "message": "Trading paused."})

    @app.route("/api/bot/resume", methods=["POST"])
    def bot_resume():
        import web_dashboard_state as wds

        wds.set_trading_paused(False)
        return jsonify({"ok": True, "paused": False, "message": "Trading resumed."})

    @app.route("/api/bot/stop", methods=["POST"])
    def bot_stop():
        import web_dashboard_state as wds

        wds.request_stop()
        return jsonify({"ok": True, "message": "Stop requested — bot will shut down gracefully."})

    @app.route("/api/bot/restart", methods=["POST"])
    def bot_restart():
        """Launch a fresh, detached bot process, then kill this process tree
        (venv launcher + uv child) so only the new bot remains running."""
        import subprocess as _sp
        import sys as _sys
        import os as _os
        import threading as _threading

        exe = _sys.executable
        argv = list(_sys.argv)  # e.g. ['src/main.py', '--web', '--web-host', ...]

        _detach = 0
        for _flag in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
            _detach |= getattr(_sp, _flag, 0)

        try:
            _log = str(PROJECT_ROOT / "logs" / "bot_restart.log")
            # Launch a DETACHED python that waits ~2s for the old bot to
            # release the port, then runs main.py with the same argv. Running
            # via a detached process keeps the new bot OUT of the old process
            # tree, so killing the old PIDs won't take it down.
            _script = (
                "import time, sys, os, runpy\n"
                f"sys.argv = {argv!r}\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))\n"
                "time.sleep(2)\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n"
            )
            _sp.Popen(
                [exe, "-c", _script],
                cwd=str(PROJECT_ROOT),
                creationflags=_detach,
                stdout=open(_log, "a", encoding="utf-8"),
                stderr=_sp.STDOUT,
            )
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        def _kill_self():
            _me = _os.getpid()
            _parent = _os.getppid()
            # Kill ONLY the old venv launcher + this (uv) bot — no /T, so the
            # new detached bot (parented to cmd.exe) is left running.
            for _pid in (_parent, _me):
                try:
                    _sp.run(["taskkill", "/PID", str(_pid), "/F"], capture_output=True)
                except Exception:
                    pass
            try:
                _os.kill(_me, 9)
            except Exception:
                pass

        _threading.Timer(0.3, _kill_self).start()
        return jsonify(
            {"ok": True, "message": "Restarting — new bot launching, this process will shut down."}
        )

    @app.route("/api/trades/clear", methods=["POST"])
    def clear_trades():
        """Pause trade logging, archive late_v3 trades to a timestamped zip,
        delete the records (disk + in-memory), then resume logging."""
        import web_dashboard_state as wds
        import zipfile
        from datetime import datetime, timezone, timedelta

        coins = ["btc", "eth", "sol"]
        trade_files = []
        for c in coins:
            p = root / "logs" / f"late_v3_{c}" / "trades.jsonl"
            if p.exists() and p.stat().st_size > 0:
                trade_files.append(p)

        # Also count in-memory closed trades (what the dashboard actually shows)
        mem_count = 0
        mt = wds.get_multi_trader()
        if mt is not None:
            for trader in getattr(mt, "traders", {}).values():
                mem_count += len(getattr(trader, "closed_trades", []) or [])

        if not trade_files and mem_count == 0:
            # Nothing to archive, but still reset capital and drop stale chart
            wds.reset_capital_and_open()
            stale_chart = root / "logs" / "pnl_chart.png"
            if stale_chart.exists():
                try:
                    stale_chart.unlink()
                except OSError as e:
                    print(f"[WEB] Failed to remove stale chart: {e}")
            return jsonify({"ok": True, "message": "No trade records to clear."})

        # Pause trade logging so in-flight writes don't pollute the archive/delete
        wds.pause_trade_logging()
        # Reset wallet to initial balance and clear open-investment total
        wds.reset_capital_and_open()
        try:
            # Brief settle to let any in-flight _log_trade calls finish/abort
            time.sleep(0.5)

            archive_name = None
            if trade_files:
                # East-8 (CST) timestamp for the archive name
                cst = timezone(timedelta(hours=8))
                stamp = datetime.now(cst).strftime("%Y%m%d_%H%M%S")
                archive_name = f"trades_{stamp}.zip"
                archive_path = root / "logs" / archive_name

                with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for p in trade_files:
                        zf.write(p, arcname=p.relative_to(root))

                # Delete the original trade records on disk
                for p in trade_files:
                    try:
                        p.write_text("")
                    except OSError as e:
                        print(f"[WEB] Failed to clear {p}: {e}")

            # Clear in-memory closed trades so the dashboard stops showing them
            if mt is not None:
                for trader in getattr(mt, "traders", {}).values():
                    trader.clear_closed_trades()

            # Force the PnL chart to regenerate from the now-empty trade files
            # (otherwise the stale png keeps showing the old trades)
            stale_chart = root / "logs" / "pnl_chart.png"
            if stale_chart.exists():
                try:
                    stale_chart.unlink()
                except OSError as e:
                    print(f"[WEB] Failed to remove stale chart: {e}")
        finally:
            wds.resume_trade_logging()

        return jsonify(
            {
                "ok": True,
                "message": f"Archived {len(trade_files)} trade file(s) and cleared {mem_count} in-memory record(s).",
                "archive": archive_name,
            }
        )

    @app.route("/pnl_chart.png")
    def pnl_chart():
        logs_dir = root / "logs"
        coins = []
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                trading = cfg.get("trading", {})
                for c in ("btc", "eth", "sol", "xrp"):
                    t = trading.get(c)
                    if isinstance(t, dict) and t.get("enabled", True):
                        coins.append(c)
        except (OSError, json.JSONDecodeError):
            coins = []
        if not coins:
            coins = ["btc", "eth", "sol", "xrp"]

        out_path = logs_dir / "pnl_chart.png"
        now = time.time()
        regenerate = True
        if out_path.exists():
            try:
                # Regenerate at most every 15s so the curve stays fresh
                regenerate = (now - out_path.stat().st_mtime) > 15
            except OSError:
                regenerate = True

        if regenerate:
            try:
                from pnl_chart_generator import generate_pnl_chart

                generate_pnl_chart(str(logs_dir), coins, str(out_path))
            except Exception as e:  # chart is non-critical
                print(f"[WEB] pnl_chart generation failed: {e}")

        if out_path.exists():
            resp = send_file(str(out_path), mimetype="image/png")
            resp.headers["Cache-Control"] = "no-store"
            return resp
        return ("", 404)

    return app


def run_server_thread(
    host: str, port: int, project_root: Path | None = None
) -> None:
    """Start Flask in a daemon thread (used by main.py --web)."""
    app = create_app(project_root or PROJECT_ROOT)

    def run():
        # Werkzeug production warning suppressed for local dashboard
        import logging

        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=run, name="WebDashboard", daemon=True)
    t.start()


if __name__ == "__main__":
    # Standalone: UI only (status from bot_state.json when bot runs with --web)
    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = create_app()
    print(f"[WEB] Open http://127.0.0.1:5050 (dashboard)")
    app.run(host="127.0.0.1", port=5050, threaded=True)
