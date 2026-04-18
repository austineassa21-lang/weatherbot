#!/usr/bin/env python3
"""
dashboard.py — WeatherBet Live Dashboard
Serves a live web dashboard reading from bot_v2.py's data files.

Add to Procfile:
    web: python dashboard.py
"""

import json
import os
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

PORT = int(os.environ.get("PORT", 8080))
DATA_DIR   = Path("data")
STATE_FILE = DATA_DIR / "state.json"
MARKETS_DIR = DATA_DIR / "markets"

# ─────────────────────────────────────────────
# DATA HELPERS
# ─────────────────────────────────────────────

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"balance": 0, "starting_balance": 0, "total_trades": 0,
                "wins": 0, "losses": 0, "peak_balance": 0}

def load_markets():
    markets = []
    if not MARKETS_DIR.exists():
        return markets
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text()))
        except Exception:
            pass
    return markets

def get_dashboard_data():
    state   = load_state()
    markets = load_markets()

    balance  = state.get("balance", 0)
    start    = state.get("starting_balance", 0)
    peak     = state.get("peak_balance", 0)
    wins     = state.get("wins", 0)
    losses   = state.get("losses", 0)
    total    = wins + losses
    pnl      = round(balance - start, 2)
    pnl_pct  = round((pnl / start * 100) if start else 0, 2)
    win_rate = round((wins / total * 100) if total else 0, 1)

    open_positions = []
    for m in markets:
        pos = m.get("position")
        if pos and pos.get("status") == "open":
            entry = pos.get("entry_price", 0)
            current = entry
            for o in m.get("all_outcomes", []):
                if o.get("market_id") == pos.get("market_id"):
                    current = o.get("bid", entry)
                    break
            unrealized = round((current - entry) * pos.get("shares", 0), 2)
            open_positions.append({
                "city":        m.get("city_name", m.get("city", "")),
                "date":        m.get("date", ""),
                "bucket":      f"{pos.get('bucket_low')}-{pos.get('bucket_high')}°",
                "entry":       entry,
                "current":     round(current, 3),
                "cost":        pos.get("cost", 0),
                "unrealized":  unrealized,
                "source":      (pos.get("forecast_src") or "").upper(),
                "hours_left":  _hours_left(m.get("event_end_date", "")),
            })

    recent_trades = []
    for m in sorted(markets, key=lambda x: x.get("date",""), reverse=True):
        pos = m.get("position")
        if not pos or pos.get("status") == "open":
            continue
        recent_trades.append({
            "city":    m.get("city_name", m.get("city", "")),
            "date":    m.get("date", ""),
            "outcome": m.get("resolved_outcome", pos.get("close_reason", "closed")),
            "pnl":     m.get("pnl") or pos.get("pnl") or 0,
            "entry":   pos.get("entry_price", 0),
            "exit":    pos.get("exit_price", 0),
            "bucket":  f"{pos.get('bucket_low')}-{pos.get('bucket_high')}°",
        })
    recent_trades = recent_trades[:20]

    return {
        "balance":        round(balance, 2),
        "start":          round(start, 2),
        "peak":           round(peak, 2),
        "pnl":            pnl,
        "pnl_pct":        pnl_pct,
        "wins":           wins,
        "losses":         losses,
        "total_trades":   total,
        "win_rate":       win_rate,
        "open_positions": open_positions,
        "recent_trades":  recent_trades,
        "updated_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

def _hours_left(end_date_str):
    try:
        from datetime import datetime, timezone
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        h = (end - datetime.now(timezone.utc)).total_seconds() / 3600
        return round(max(0, h), 1)
    except Exception:
        return None

# ─────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WeatherBet Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:       #0a0e14;
    --surface:  #111820;
    --border:   #1e2d3d;
    --accent:   #00d4ff;
    --green:    #00ff88;
    --red:      #ff4466;
    --yellow:   #ffd166;
    --text:     #c8d8e8;
    --muted:    #4a6278;
    --glow:     0 0 20px rgba(0,212,255,0.15);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Mono', monospace;
    min-height: 100vh;
    padding: 0;
  }

  /* HEADER */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 20px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .logo {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 1.4rem;
    color: var(--accent);
    letter-spacing: -0.02em;
  }
  .logo span { color: var(--text); }
  .live-badge {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.7rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .live-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(0,255,136,0.4); }
    50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(0,255,136,0); }
  }

  /* MAIN */
  main { padding: 28px 32px; max-width: 1400px; margin: 0 auto; }

  /* STAT CARDS */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 24px;
    position: relative;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .stat-card:hover { border-color: var(--accent); box-shadow: var(--glow); }
  .stat-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--accent), transparent);
  }
  .stat-label {
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .stat-value {
    font-family: 'Syne', sans-serif;
    font-weight: 800;
    font-size: 1.8rem;
    color: #fff;
    line-height: 1;
  }
  .stat-sub {
    font-size: 0.7rem;
    color: var(--muted);
    margin-top: 6px;
  }
  .positive { color: var(--green) !important; }
  .negative { color: var(--red) !important; }
  .neutral  { color: var(--yellow) !important; }

  /* SECTIONS */
  .section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 20px;
    overflow: hidden;
  }
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
  }
  .section-title {
    font-family: 'Syne', sans-serif;
    font-weight: 700;
    font-size: 0.85rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--accent);
  }
  .section-count {
    font-size: 0.7rem;
    color: var(--muted);
    background: var(--border);
    padding: 3px 10px;
    border-radius: 20px;
  }

  /* TABLE */
  table { width: 100%; border-collapse: collapse; }
  th {
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
    padding: 10px 24px;
    text-align: left;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 12px 24px;
    font-size: 0.78rem;
    border-bottom: 1px solid rgba(30,45,61,0.5);
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(0,212,255,0.03); }

  .city-name { font-weight: 700; color: #fff; }
  .bucket-badge {
    display: inline-block;
    background: rgba(0,212,255,0.1);
    border: 1px solid rgba(0,212,255,0.2);
    color: var(--accent);
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.7rem;
  }
  .outcome-badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .outcome-win    { background: rgba(0,255,136,0.15); color: var(--green); }
  .outcome-loss   { background: rgba(255,68,102,0.15); color: var(--red); }
  .outcome-stop   { background: rgba(255,209,102,0.15); color: var(--yellow); }
  .outcome-closed { background: rgba(74,98,120,0.3); color: var(--muted); }

  .hours-bar {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .hours-pip {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--green);
  }
  .hours-pip.soon { background: var(--yellow); }
  .hours-pip.urgent { background: var(--red); }

  /* EMPTY STATE */
  .empty {
    padding: 40px 24px;
    text-align: center;
    color: var(--muted);
    font-size: 0.8rem;
  }

  /* UPDATED */
  .updated-at {
    text-align: center;
    color: var(--muted);
    font-size: 0.65rem;
    padding: 20px;
    letter-spacing: 0.05em;
  }

  /* REFRESH BTN */
  .refresh-btn {
    background: none;
    border: 1px solid var(--border);
    color: var(--muted);
    font-family: 'Space Mono', monospace;
    font-size: 0.65rem;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    transition: all 0.2s;
  }
  .refresh-btn:hover {
    border-color: var(--accent);
    color: var(--accent);
  }

  @media (max-width: 768px) {
    main { padding: 16px; }
    header { padding: 16px; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    th, td { padding: 10px 14px; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">Weather<span>Bet</span></div>
  <div style="display:flex;align-items:center;gap:16px;">
    <button class="refresh-btn" onclick="location.reload()">↻ Refresh</button>
    <div class="live-badge">
      <div class="live-dot"></div>
      <span id="updated-at">Loading...</span>
    </div>
  </div>
</header>

<main>

  <!-- STAT CARDS -->
  <div class="stats-grid" id="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Balance</div>
      <div class="stat-value" id="stat-balance">—</div>
      <div class="stat-sub" id="stat-start">Started —</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total P&L</div>
      <div class="stat-value" id="stat-pnl">—</div>
      <div class="stat-sub" id="stat-pnl-pct">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value" id="stat-winrate">—</div>
      <div class="stat-sub" id="stat-wl">— W / — L</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Peak Balance</div>
      <div class="stat-value" id="stat-peak">—</div>
      <div class="stat-sub" id="stat-trades">— total trades</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value" id="stat-open">—</div>
      <div class="stat-sub">Active right now</div>
    </div>
  </div>

  <!-- OPEN POSITIONS -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Open Positions</div>
      <div class="section-count" id="open-count">0</div>
    </div>
    <div id="open-body">
      <div class="empty">Loading...</div>
    </div>
  </div>

  <!-- RECENT TRADES -->
  <div class="section">
    <div class="section-header">
      <div class="section-title">Recent Trades</div>
      <div class="section-count" id="trades-count">0</div>
    </div>
    <div id="trades-body">
      <div class="empty">Loading...</div>
    </div>
  </div>

  <div class="updated-at" id="footer-updated"></div>

</main>

<script>
async function loadData() {
  try {
    const res = await fetch('/api/data');
    const d = await res.json();

    // Stats
    document.getElementById('stat-balance').textContent = '$' + d.balance.toLocaleString('en-US', {minimumFractionDigits:2});
    document.getElementById('stat-start').textContent = 'Started $' + d.start.toLocaleString('en-US', {minimumFractionDigits:2});

    const pnlEl = document.getElementById('stat-pnl');
    pnlEl.textContent = (d.pnl >= 0 ? '+$' : '-$') + Math.abs(d.pnl).toFixed(2);
    pnlEl.className = 'stat-value ' + (d.pnl >= 0 ? 'positive' : 'negative');

    document.getElementById('stat-pnl-pct').textContent = (d.pnl_pct >= 0 ? '+' : '') + d.pnl_pct + '% return';

    const wrEl = document.getElementById('stat-winrate');
    wrEl.textContent = d.win_rate + '%';
    wrEl.className = 'stat-value ' + (d.win_rate >= 50 ? 'positive' : d.win_rate > 0 ? 'neutral' : '');
    document.getElementById('stat-wl').textContent = d.wins + ' W / ' + d.losses + ' L';

    document.getElementById('stat-peak').textContent = '$' + d.peak.toLocaleString('en-US', {minimumFractionDigits:2});
    document.getElementById('stat-trades').textContent = d.total_trades + ' total trades';
    document.getElementById('stat-open').textContent = d.open_positions.length;

    document.getElementById('updated-at').textContent = d.updated_at;
    document.getElementById('footer-updated').textContent = 'Last updated: ' + d.updated_at;
    document.getElementById('open-count').textContent = d.open_positions.length;
    document.getElementById('trades-count').textContent = d.recent_trades.length;

    // Open positions
    const openBody = document.getElementById('open-body');
    if (d.open_positions.length === 0) {
      openBody.innerHTML = '<div class="empty">No open positions</div>';
    } else {
      let html = '<table><thead><tr>' +
        '<th>City</th><th>Date</th><th>Bucket</th><th>Entry</th><th>Current</th><th>Cost</th><th>Unrealized</th><th>Source</th><th>Hours Left</th>' +
        '</tr></thead><tbody>';
      for (const p of d.open_positions) {
        const unr = p.unrealized;
        const unrClass = unr >= 0 ? 'positive' : 'negative';
        const unrStr = (unr >= 0 ? '+$' : '-$') + Math.abs(unr).toFixed(2);
        const h = p.hours_left;
        const pipClass = h === null ? '' : h < 6 ? 'urgent' : h < 24 ? 'soon' : '';
        const hoursStr = h === null ? '—' : h + 'h';
        html += `<tr>
          <td><span class="city-name">${p.city}</span></td>
          <td>${p.date}</td>
          <td><span class="bucket-badge">${p.bucket}</span></td>
          <td>$${p.entry.toFixed(3)}</td>
          <td>$${p.current.toFixed(3)}</td>
          <td>$${p.cost.toFixed(2)}</td>
          <td class="${unrClass}">${unrStr}</td>
          <td>${p.source}</td>
          <td><div class="hours-bar"><div class="hours-pip ${pipClass}"></div>${hoursStr}</div></td>
        </tr>`;
      }
      html += '</tbody></table>';
      openBody.innerHTML = html;
    }

    // Recent trades
    const tradesBody = document.getElementById('trades-body');
    if (d.recent_trades.length === 0) {
      tradesBody.innerHTML = '<div class="empty">No closed trades yet</div>';
    } else {
      let html = '<table><thead><tr>' +
        '<th>City</th><th>Date</th><th>Bucket</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Outcome</th>' +
        '</tr></thead><tbody>';
      for (const t of d.recent_trades) {
        const pnl = t.pnl || 0;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';
        const pnlStr = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2);
        const outcome = t.outcome || 'closed';
        let badgeClass = 'outcome-closed';
        if (outcome === 'win') badgeClass = 'outcome-win';
        else if (outcome === 'loss') badgeClass = 'outcome-loss';
        else if (outcome.includes('stop') || outcome.includes('take')) badgeClass = 'outcome-stop';
        html += `<tr>
          <td><span class="city-name">${t.city}</span></td>
          <td>${t.date}</td>
          <td><span class="bucket-badge">${t.bucket}</span></td>
          <td>$${(t.entry||0).toFixed(3)}</td>
          <td>${t.exit ? '$'+t.exit.toFixed(3) : '—'}</td>
          <td class="${pnlClass}">${pnlStr}</td>
          <td><span class="outcome-badge ${badgeClass}">${outcome.replace('_',' ')}</span></td>
        </tr>`;
      }
      html += '</tbody></table>';
      tradesBody.innerHTML = html;
    }

  } catch(e) {
    console.error(e);
  }
}

loadData();
// Auto-refresh every 60 seconds
setInterval(loadData, 60000);
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────
# HTTP SERVER
# ─────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/data":
            data = json.dumps(get_dashboard_data()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/", "/dashboard"):
            html = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(html))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Dashboard running on port {PORT}")
    print(f"  http://localhost:{PORT}")
    server.serve_forever()
