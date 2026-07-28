(function () {
  const summaryEl = document.getElementById("summary-stats");
  const coinsEl = document.getElementById("coins-container");
  const tbody = document.querySelector("#recent-trades tbody");
  const badge = document.getElementById("conn-badge");
  const configEditor = document.getElementById("config-editor");
  const configMsg = document.getElementById("config-message");
  const headerSubtitle = document.getElementById("header-subtitle");
  const chartImg = document.getElementById("pnl-chart");
  const chartEmpty = document.getElementById("pnl-chart-empty");

  function refreshChart() {
    if (!chartImg) return;
    chartImg.onerror = () => {
      chartImg.style.display = "none";
      if (chartEmpty) chartEmpty.style.display = "block";
    };
    chartImg.onload = () => {
      chartImg.style.display = "block";
      if (chartEmpty) chartEmpty.style.display = "none";
    };
    chartImg.src = "/pnl_chart.png?t=" + Date.now();
  }

  function labelFromIntervalSec(sec) {
    if (sec == null || Number.isNaN(sec)) return null;
    if (sec === 300) return "5m";
    if (sec === 900) return "15m";
    return `${sec}s`;
  }

  function updateHeaderSubtitle(data) {
    if (!headerSubtitle) return;
    let ml = data.market_label;
    if (!ml && data.market_interval_sec != null) {
      ml = labelFromIntervalSec(data.market_interval_sec);
    }
    const part = ml ? `${ml} ` : "";
    headerSubtitle.textContent = `Polymarket ${part}desk · live status · settings · analytics`;
  }

  function updateHeaderSubtitleFromConfig(cfg) {
    if (!headerSubtitle || !cfg || typeof cfg !== "object") return;
    const pm = cfg.data_sources && cfg.data_sources.polymarket;
    if (!pm) return;
    let ml = pm.market_window;
    if (!ml && pm.market_interval_sec != null) {
      ml = labelFromIntervalSec(pm.market_interval_sec);
    }
    if (ml) {
      headerSubtitle.textContent = `Polymarket ${ml} desk · live status · settings · analytics`;
    }
  }

  function fmtTime(sec) {
    sec = Math.floor(sec || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    return `${m}m ${s}s`;
  }

  function fmtUsd(n) {
    if (n == null || Number.isNaN(n)) return "—";
    const sign = n >= 0 ? "+" : "";
    return sign + "$" + Number(n).toFixed(2);
  }

  function renderSummary(data) {
    const p = data.portfolio || {};
    const dry = data.dry_run;
    summaryEl.innerHTML = [
      card("Uptime", fmtTime(data.uptime_sec)),
      card("Market", data.market_label || "—"),
      card("Mode", dry ? "DRY RUN" : "LIVE", dry ? "warn" : "ok"),
      card("剩余资金", data.wallet_balance != null ? "$" + data.wallet_balance.toFixed(2) : "—"),
      card("Total PnL", fmtUsd(p.total_pnl), (p.total_pnl || 0) >= 0 ? "pos" : "neg"),
      card("Trades", String(p.total_trades ?? "0")),
      card("ROI %", (p.portfolio_roi != null ? p.portfolio_roi.toFixed(2) + "%" : "—"), (p.portfolio_roi || 0) >= 0 ? "pos" : "neg"),
    ].join("");
  }

  function card(label, value, valClass) {
    const vc = valClass ? ` ${valClass}` : "";
    return `<div class="stat-card"><div class="label">${label}</div><div class="value${vc}">${escapeHtml(
      String(value)
    )}</div></div>`;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function renderCoins(data) {
    const coins = data.coins || {};
    const names = ["btc", "eth", "sol", "xrp"];
    coinsEl.innerHTML = names
      .map((c) => {
        const x = coins[c];
        if (!x) return "";
        const en = x.trading_enabled !== false;
        const fav = x.favorite || "—";
        const conf = x.confidence != null ? x.confidence.toFixed(3) : "—";
        const slugShort = (x.market_slug || "").split("-").pop() || "—";
        const st = x.stats || {};
        let posHtml = '<div class="row"><span>Position</span><strong>None</strong></div>';
        if (x.position) {
          const p = x.position;
          posHtml = `
          <div class="pos-block">
            <div class="row"><span>Unrealized</span><strong class="${p.unrealized_pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(
            p.unrealized_pnl
          )}</strong></div>
            <div class="row"><span>Invested</span><strong>$${p.total_invested}</strong></div>
            <div class="row"><span>Side / entries</span><strong>${p.our_side} · ${p.entries_count}</strong></div>
            <div class="row"><span>If UP wins</span><strong>${fmtUsd(p.if_up_wins)}</strong></div>
            <div class="row"><span>If DOWN wins</span><strong>${fmtUsd(p.if_down_wins)}</strong></div>
          </div>`;
        }
        return `
        <div class="coin-card">
          <h3>${c.toUpperCase()}
            ${en ? "" : '<span class="disabled-tag">disabled</span>'}
          </h3>
          <div class="row"><span>Market</span><strong>${escapeHtml(slugShort)}</strong></div>
          <div class="row"><span>Time left</span><strong>${fmtTime(x.seconds_till_end)}</strong></div>
          <div class="row"><span>UP / DN ask</span><strong>${x.up_ask?.toFixed(3) ?? "—"} / ${x.down_ask?.toFixed(
            3
          ) ?? "—"}</strong></div>
          <div class="row"><span>Favorite · Conf</span><strong>${fav} · ${conf}</strong></div>
          <div class="row"><span>PnL (coin)</span><strong class="${(st.pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(
            st.pnl
          )}</strong></div>
          <div class="row"><span>W/L · WR</span><strong>${st.wins ?? 0}/${st.losses ?? 0} · ${st.win_rate ?? 0}%</strong></div>
          ${posHtml}
        </div>`;
      })
      .join("");
  }

  function renderRecent(data) {
    const rows = data.recent_trades || [];
    const limitSel = document.getElementById("recent-limit");
    const limit = limitSel ? parseInt(limitSel.value, 10) || 10 : 10;
    const shown = rows.slice(0, limit);
    tbody.innerHTML = shown
      .map((t) => {
        const pnl = t.pnl;
        const cls = pnl >= 0 ? "pnl-pos" : "pnl-neg";
        const m = t.market_slug || "";
        const exitDt = t.datetime || "";
        return `<tr>
        <td>${escapeHtml(m || "")}</td>
        <td>${escapeHtml(t.entry_timestamp || "")}</td>
        <td>${escapeHtml(exitDt)}</td>
        <td>${escapeHtml(String(t.winner ?? ""))}</td>
        <td>${t.entry_price != null ? "$" + Number(t.entry_price).toFixed(3) : "—"}</td>
        <td>${t.contracts != null ? Number(t.contracts).toFixed(1) : "—"}</td>
        <td>${t.amount != null ? "$" + Number(t.amount).toFixed(2) : "—"}</td>
        <td class="${cls}">${fmtUsd(pnl)}</td>
      </tr>`;
      })
      .join("");
    if (!shown.length) {
      tbody.innerHTML = '<tr><td colspan="8">No closed trades this session yet</td></tr>';
    }
  }

  async function fetchStatus() {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("status " + r.status);
    return r.json();
  }

  async function tick() {
    try {
      const data = await fetchStatus();
      updateHeaderSubtitle(data);
      renderSummary(data);
      renderCoins(data);
      renderRecent(data);
      refreshChart();
      const health = await fetch("/api/health", { cache: "no-store" }).then((x) => x.json());
      if (health.bot_live) {
        badge.textContent = "live";
        badge.className = "badge badge-ok";
      } else {
        badge.textContent = "no live bot";
        badge.className = "badge badge-off";
      }
    } catch (e) {
      badge.textContent = "disconnected";
      badge.className = "badge badge-warn";
    }
  }

  async function loadConfig() {
    configMsg.textContent = "";
    configMsg.className = "message";
    try {
      const r = await fetch("/api/config");
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      configEditor.value = JSON.stringify(j, null, 2);
      updateHeaderSubtitleFromConfig(j);
      configMsg.textContent = "Loaded.";
      configMsg.className = "message ok";
    } catch (e) {
      configMsg.textContent = String(e.message || e);
      configMsg.className = "message err";
    }
  }

  const btnRefresh = document.getElementById("btn-refresh");
  if (btnRefresh) btnRefresh.addEventListener("click", tick);

  const btnRestart = document.getElementById("btn-restart");
  if (btnRestart) {
    btnRestart.addEventListener("click", async () => {
      if (!confirm("确定重启 bot？\n将启动新进程并关闭当前进程（配置改动会自动生效）。")) return;
      btnRestart.disabled = true;
      btnRestart.textContent = "重启中…";
      try {
        const r = await fetch("/api/bot/restart", { method: "POST" });
        const j = await r.json();
        if (!r.ok || !j.ok) throw new Error(j.error || "restart failed");
        alert("已发送重启指令，新 bot 正在启动，本页将在几秒后断开。");
      } catch (e) {
        alert("重启失败: " + e);
        btnRestart.disabled = false;
        btnRestart.textContent = "重启";
      }
    });
  }

  const recentLimit = document.getElementById("recent-limit");
  if (recentLimit) recentLimit.addEventListener("change", tick);
  document.getElementById("btn-load-config").addEventListener("click", loadConfig);
  document.getElementById("btn-save-config").addEventListener("click", async () => {
    configMsg.textContent = "";
    try {
      const parsed = JSON.parse(configEditor.value);
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "save failed");
      configMsg.textContent = j.message || "Saved.";
      configMsg.className = "message ok";
    } catch (e) {
      configMsg.textContent = String(e.message || e);
      configMsg.className = "message err";
    }
  });

  const btnClear = document.getElementById("btn-clear-trades");
  if (btnClear) {
    btnClear.addEventListener("click", async () => {
      if (!confirm("清除记录？\n将打包并删除所有交易记录（late_v3 BTC/ETH/SOL）。\n此操作不可撤销。")) return;
      btnClear.disabled = true;
      btnClear.textContent = "处理中…";
      try {
        const r = await fetch("/api/trades/clear", { method: "POST" });
        const j = await r.json();
        if (!r.ok) throw new Error(j.error || "clear failed");
        alert(j.message || "已清除");
        tick();
      } catch (e) {
        alert(String(e.message || e));
      } finally {
        btnClear.disabled = false;
        btnClear.textContent = "清除记录";
      }
    });
  }

  loadConfig();
  tick();
  setInterval(tick, 1200);
})();
