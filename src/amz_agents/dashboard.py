"""Agent tracker: one page showing what every agent has done, what is waiting
on whom, and whether each agent is healthy.

  amz-agents dashboard                  write reports/dashboard.html (also done by `run`)
  amz-agents dashboard --serve          serve it at http://127.0.0.1:8765, always fresh
  amz-agents dashboard --serve --allow-actions
                                        ...with Approve / Reject / Mark done buttons

The page is self-contained (no external scripts). Data goes in as JSON and is
written into the page with textContent only, so text from reviews, CRMs or ad
names can't inject markup. The server binds to localhost by default, and with
--allow-actions every change needs the per-process token embedded in the page.
"""

from __future__ import annotations

import json
import secrets
from collections import Counter
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from .actions import Action, ActionLog
from .agents.base import EFFORT_HOURS, TEAMS
from .agents.chief_of_staff import _describe
from .data.warehouse import Warehouse

WINDOW_DAYS = 30
STALE_WAITING_DAYS = 3

# Where an action is in its life, for progress bars and filters.
STAGE = {
    "proposed": "waiting", "pending_approval": "waiting",
    "approved": "in_progress", "acknowledged": "in_progress",
    "executed": "done", "done": "done",
    "rejected": "rejected", "failed": "failed",
}


def _agents() -> list[tuple[str, Any]]:
    from .orchestrator import DEFAULT_AGENTS

    return [(a.name, a.role) for a in DEFAULT_AGENTS]


def collect(wh: Warehouse, today: date | None = None) -> dict[str, Any]:
    """Everything the page shows, as plain JSON-able data."""
    today = today or date.today()
    run = (wh.query("SELECT * FROM runs ORDER BY id DESC LIMIT 1") or [None])[0]
    health = {r["agent"]: r for r in wh.query(
        "SELECT * FROM agent_runs WHERE run_id=?", (run["id"],))} if run else {}
    last_data = wh.query("SELECT MAX(date) AS d FROM metrics_daily")[0]["d"]

    rows = wh.query(
        "SELECT * FROM actions WHERE created >= date(?, ?) "
        "OR status IN ('pending_approval', 'approved', 'acknowledged') ORDER BY id DESC",
        (today.isoformat(), f"-{WINDOW_DAYS} days"),
    )
    verdicts = {o["action_id"]: o for o in wh.query("SELECT * FROM outcomes")}
    actions = []
    for r in rows:
        before, after = json.loads(r["before"] or "null"), json.loads(r["after"] or "null")
        a = Action(agent=r["agent"], kind=r["kind"], target=r["target"], before=before,
                   after=after, reason=r["reason"] or "", sku=r["sku"])
        try:
            title = (f"Decision needed: {r['target']}" if r["kind"] == "escalation"
                     else _describe(a))
        except (KeyError, TypeError, AttributeError):
            title = f"{r['kind']} {r['target']}"
        created = (r["created"] or today.isoformat())[:10]
        o = verdicts.get(r["id"])
        actions.append({
            "id": r["id"], "agent": r["agent"], "team": r["team"] or "", "kind": r["kind"],
            "title": title, "reason": r["reason"] or "", "notes": r["notes"] or "",
            "status": r["status"], "stage": STAGE.get(r["status"], "waiting"),
            "priority": r["priority"] or "", "impact": r["impact_usd"], "effort": r["effort"] or "",
            "roi": r["roi"], "sku": r["sku"] or "", "created": created,
            "age": (today - date.fromisoformat(created)).days,
            "verdict": o["verdict"] if o else None,
        })

    agents = []
    for name, role in _agents():
        mine = [a for a in actions if a["agent"] == name]
        stages = Counter(a["stage"] for a in mine)
        waiting = [a["age"] for a in mine if a["stage"] == "waiting"]
        h = health.get(name)
        problems = json.loads(h["problems"]) if h else ["hasn't run yet"]
        agents.append({
            "name": name, "team": role.team, "team_label": TEAMS.get(role.team, role.team),
            "mission": role.mission, "proposed_last_run": h["proposed"] if h else 0,
            "problems": problems, "stages": dict(stages),
            "helped": sum(1 for a in mine if a["verdict"] == "helped"),
            "hurt": sum(1 for a in mine if a["verdict"] == "hurt"),
            "oldest_waiting": max(waiting) if waiting else None,
        })
    chief_items = [a for a in actions if a["agent"] == "chief_of_staff"]

    open_impact = sum(a["impact"] or 0 for a in actions
                      if a["stage"] == "waiting" and (a["impact"] or 0) > 0)
    stage_counts = Counter(a["stage"] for a in actions)
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "today": today.isoformat(),
        "run": {"finished": run["finished"], "mode": run["mode"], "submitted": run["submitted"],
                "skipped": run["skipped"]} if run else None,
        "data_through": last_data,
        "kpis": {
            "waiting": stage_counts["waiting"], "waiting_impact": round(open_impact),
            "in_progress": stage_counts["in_progress"], "done": stage_counts["done"],
            "failed": stage_counts["failed"],
            "healthy": sum(1 for a in agents if not a["problems"]), "agents": len(agents),
            "escalations": sum(1 for a in chief_items if a["stage"] == "waiting"),
        },
        "agents": agents,
        "teams": TEAMS,
        "actions": actions,
        "effort_hours": EFFORT_HOURS,
        "window_days": WINDOW_DAYS,
        "stale_days": STALE_WAITING_DAYS,
    }


def render(data: dict[str, Any], token: str | None = None) -> str:
    """The page. `token` turns on the action buttons (server mode only)."""
    payload = json.dumps({**data, "token": token}).replace("</", "<\\/")
    return _PAGE.replace("__DATA__", payload)


def write(wh: Warehouse, out: str | Path = "reports/dashboard.html") -> Path:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(collect(wh)))
    return path


def serve(wh: Warehouse, log: ActionLog, host: str = "127.0.0.1", port: int = 8765,
          allow_actions: bool = False) -> None:
    token = secrets.token_urlsafe(24) if allow_actions else None
    ops = {"approve": log.approve, "reject": log.reject, "done": log.complete}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.split("?")[0] not in ("/", "/index.html"):
                return self._send(404, b"not found", "text/plain")
            self._send(200, render(collect(wh), token).encode(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if not token or self.headers.get("X-Tracker-Token") != token:
                return self._send(403, b'{"error":"actions are off"}', "application/json")
            try:
                n = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(n) or b"{}")
                ops[req["op"]](int(req["id"]))
                status = wh.query("SELECT status FROM actions WHERE id=?", (int(req["id"]),))
                body = {"id": req["id"], "status": status[0]["status"]}
                self._send(200, json.dumps(body).encode(), "application/json")
            except (KeyError, ValueError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *args: Any) -> None:
            pass

    print(f"Agent tracker at http://{host}:{port}"
          + (" (approve/reject/done enabled)" if token else " (read-only)"))
    HTTPServer((host, port), Handler).serve_forever()


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent tracker</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --surface-2: #f0efec; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #75746f;
  --accent: #2a78d6; --accent-soft: #e6f0fc;
  --st-waiting: #86b6ef; --st-progress: #2a78d6; --st-done: #0ca30c;
  --st-rejected: #b9b8b2; --st-failed: #d03b3b;
  --good: #0ca30c; --good-text: #006300; --warn: #fab219; --warn-text: #8a5a00;
  --crit: #d03b3b; --crit-text: #b02a2a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
    --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
    --accent: #3987e5; --accent-soft: #1d2a3a;
    --st-waiting: #6da7ec; --st-progress: #3987e5; --st-done: #0ca30c;
    --st-rejected: #5e5d58; --st-failed: #e66767;
    --good-text: #4cc24c; --warn-text: #fab219; --crit-text: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
  --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
  --accent: #3987e5; --accent-soft: #1d2a3a;
  --st-waiting: #6da7ec; --st-progress: #3987e5; --st-done: #0ca30c;
  --st-rejected: #5e5d58; --st-failed: #e66767;
  --good-text: #4cc24c; --warn-text: #fab219; --crit-text: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
.wrap { max-width: 1200px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; margin-bottom: 20px; }
h1 { font-size: 22px; margin: 0; font-weight: 650; }
h2 { font-size: 16px; margin: 32px 0 12px; font-weight: 650; }
.meta { color: var(--text-2); font-size: 13px; display: flex; flex-wrap: wrap; gap: 6px 14px; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px;
  background: var(--surface-2); color: var(--text-2); font-size: 12px; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.tile .label { color: var(--text-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 650; margin-top: 4px; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--text-3); font-size: 12px; margin-top: 2px; }
.tile.alert .value { color: var(--crit-text); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--text-2); font-size: 12px; margin: 0 0 12px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.agents { display: grid; grid-template-columns: repeat(auto-fill, minmax(270px, 1fr)); gap: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px;
  cursor: pointer; text-align: left; color: inherit; font: inherit; width: 100%; }
.card:hover, .card:focus-visible { border-color: var(--accent); outline: none; }
.card.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.card .top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.card .name { font-weight: 650; font-size: 15px; text-transform: capitalize; }
.card .team { color: var(--text-3); font-size: 12px; }
.card .mission { color: var(--text-2); font-size: 12.5px; margin: 6px 0 10px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.status-ok { color: var(--good-text); } .status-bad { color: var(--crit-text); }
.bar { display: flex; height: 10px; border-radius: 4px; overflow: hidden; background: var(--surface-2); gap: 2px; }
.bar div { height: 100%; min-width: 3px; }
.bar div:first-child { border-radius: 4px 0 0 4px; } .bar div:last-child { border-radius: 0 4px 4px 0; }
.bar div:only-child { border-radius: 4px; }
.counts { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--text-2); margin-top: 8px; }
.counts b { color: var(--text); font-variant-numeric: tabular-nums; }
.foot { display: flex; justify-content: space-between; gap: 8px; margin-top: 8px; font-size: 12px; color: var(--text-3); }
.foot .warn { color: var(--warn-text); }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
select, input[type=search] { font: inherit; font-size: 13px; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; }
input[type=search] { min-width: 200px; flex: 1; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { font: inherit; font-size: 13px; padding: 6px 10px; border: 0; background: var(--surface);
  color: var(--text-2); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button[aria-pressed=true] { background: var(--accent-soft); color: var(--text); font-weight: 600; }
.clear { font: inherit; font-size: 13px; background: none; border: 0; color: var(--accent); cursor: pointer; }
.table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--text-2); padding: 10px 12px; border-bottom: 1px solid var(--border);
  white-space: nowrap; position: sticky; top: 0; background: var(--surface); }
td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: 0; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
th.num { text-align: right; }
.title { font-weight: 550; }
.why { color: var(--text-2); font-size: 12px; margin-top: 2px; max-width: 560px; }
.pri { font-size: 12px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); white-space: nowrap; }
.pri-now { border-color: var(--crit); color: var(--crit-text); font-weight: 600; }
.pri-high { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.stale { color: var(--warn-text); font-weight: 600; }
.btns { display: flex; gap: 6px; }
.btns button { font: inherit; font-size: 12px; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--text); }
.btns button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.empty { padding: 24px; text-align: center; color: var(--text-2); }
.note { color: var(--text-3); font-size: 12px; margin-top: 10px; }
#tip { position: fixed; pointer-events: none; background: var(--text); color: var(--bg); font-size: 12px;
  padding: 6px 8px; border-radius: 6px; display: none; z-index: 10; white-space: nowrap; }
@media (max-width: 640px) { .hide-sm { display: none; } input[type=search] { min-width: 100%; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Agent tracker</h1>
    <div class="meta" id="meta"></div>
  </header>
  <section class="kpis" id="kpis" aria-label="Summary"></section>

  <h2>Agents</h2>
  <div class="legend" id="legend"></div>
  <section class="agents" id="agents" aria-label="Agents"></section>
  <p class="note" id="window-note"></p>

  <h2 id="queue-title">Work queue</h2>
  <div class="filters">
    <select id="f-team" aria-label="Team"></select>
    <select id="f-agent" aria-label="Agent"></select>
    <div class="seg" id="f-stage" role="group" aria-label="Status"></div>
    <select id="f-priority" aria-label="Priority"></select>
    <input type="search" id="f-q" placeholder="Search actions" aria-label="Search actions">
    <button class="clear" id="f-clear" type="button">Clear filters</button>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th class="num">#</th><th>Action</th><th>Agent</th><th class="hide-sm">Team</th><th>Priority</th>
        <th class="num">Est. impact</th><th class="hide-sm">Effort</th><th class="num hide-sm">ROI</th>
        <th>Status</th><th class="num">Age</th><th id="th-act"></th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <p class="note" id="queue-note"></p>
</div>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
  const D = JSON.parse(document.getElementById('data').textContent);
  const STAGES = [
    ['done', 'Done', 'var(--st-done)'],
    ['in_progress', 'In progress', 'var(--st-progress)'],
    ['waiting', 'Waiting for review', 'var(--st-waiting)'],
    ['rejected', 'Rejected', 'var(--st-rejected)'],
    ['failed', 'Failed', 'var(--st-failed)'],
  ];
  const STAGE_LABEL = Object.fromEntries(STAGES.map(s => [s[0], s[1]]));
  const STAGE_COLOR = Object.fromEntries(STAGES.map(s => [s[0], s[2]]));
  const STATUS_LABEL = {proposed: 'Suggested', pending_approval: 'Waiting for approval',
    approved: 'Approved', acknowledged: 'In progress (person)', executed: 'Done (by API)',
    done: 'Done', rejected: 'Rejected', failed: 'Failed'};
  const PRI_ORDER = {now: 0, high: 1, medium: 2, low: 3, judgment: 4, '': 5};
  const AGENT_LABEL = n => n === 'chief_of_staff' ? 'Chief of staff' : n === 'ux' ? 'UX' : n[0].toUpperCase() + n.slice(1);
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    kids.flat().forEach(k => k != null && e.append(k));
    return e;
  };
  const usd = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(Math.round(v)).toLocaleString();
  const fmt = n => n.toLocaleString();
  const state = {team: '', agent: '', stage: '', priority: '', q: ''};
  const tip = document.getElementById('tip');
  const showTip = (e, text) => { tip.textContent = text; tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + 'px';
    tip.style.top = (e.clientY + 14) + 'px'; };
  const hideTip = () => { tip.style.display = 'none'; };

  // Header
  const meta = document.getElementById('meta');
  if (D.run) {
    meta.append(el('span', {class: 'chip', text: 'Mode: ' + D.run.mode}),
      el('span', {text: 'Last run ' + D.run.finished + ' UTC'}),
      el('span', {text: fmt(D.run.submitted) + ' items queued, ' + fmt(D.run.skipped) + ' duplicates skipped'}));
  } else meta.append(el('span', {text: 'No runs yet: run amz-agents run'}));
  meta.append(el('span', {text: 'Sales data through ' + (D.data_through || 'n/a')}));

  // KPI tiles
  const K = D.kpis;
  const tile = (label, value, sub, alert) => el('div', {class: 'tile' + (alert ? ' alert' : '')},
    el('div', {class: 'label', text: label}), el('div', {class: 'value', text: value}),
    sub ? el('div', {class: 'sub', text: sub}) : null);
  document.getElementById('kpis').append(
    tile('Waiting for review', fmt(K.waiting), usd(K.waiting_impact) + ' est. 30-day impact'
         + (K.escalations ? ' · ' + K.escalations + ' escalations' : '')),
    tile('In progress', fmt(K.in_progress), 'approved, being carried out'),
    tile('Done', fmt(K.done), 'last ' + D.window_days + ' days'),
    tile('Failed', fmt(K.failed), K.failed ? 'needs a look' : 'none', K.failed > 0),
    tile('Agents healthy', K.healthy + ' / ' + K.agents, K.healthy < K.agents ? 'see cards below' : 'all running with data',
         K.healthy < K.agents));

  // Legend
  document.getElementById('legend').append(...STAGES.map(([, label, color]) =>
    el('span', {}, el('i', {class: 'dot', style: 'background:' + color}), label)));
  document.getElementById('window-note').textContent =
    'Bars count each agent\'s actions from the last ' + D.window_days + ' days, plus anything still open. Click an agent to filter the queue.';

  // Agent cards
  const agentsBox = document.getElementById('agents');
  const cards = {};
  for (const a of D.agents) {
    const total = STAGES.reduce((n, [k]) => n + (a.stages[k] || 0), 0);
    const bar = el('div', {class: 'bar', role: 'img',
      'aria-label': STAGES.filter(([k]) => a.stages[k]).map(([k, l]) => l + ' ' + a.stages[k]).join(', ') || 'No actions'});
    for (const [k, label, color] of STAGES) {
      const n = a.stages[k] || 0;
      if (!n) continue;
      const seg = el('div', {style: 'flex:' + n + ';background:' + color});
      seg.addEventListener('mousemove', e => showTip(e, label + ': ' + n + ' of ' + total));
      seg.addEventListener('mouseleave', hideTip);
      bar.append(seg);
    }
    const ok = a.problems.length === 0;
    const counts = el('div', {class: 'counts'},
      ...STAGES.filter(([k]) => a.stages[k]).map(([k, l]) => el('span', {}, el('b', {text: a.stages[k]}), ' ' + l.toLowerCase())));
    if (!total) counts.append(el('span', {text: 'No actions yet'}));
    const foot = el('div', {class: 'foot'},
      el('span', {text: a.proposed_last_run + ' proposed last run'}),
      a.oldest_waiting != null && a.oldest_waiting >= D.stale_days
        ? el('span', {class: 'warn', text: 'oldest waiting ' + a.oldest_waiting + ' days'})
        : (a.helped || a.hurt) ? el('span', {text: a.helped + ' helped · ' + a.hurt + ' hurt'}) : el('span'));
    const card = el('button', {class: 'card', type: 'button', 'aria-pressed': 'false',
      onclick: () => { state.agent = state.agent === a.name ? '' : a.name; sync(); }},
      el('div', {class: 'top'},
        el('div', {}, el('div', {class: 'name', text: AGENT_LABEL(a.name)}), el('div', {class: 'team', text: a.team_label})),
        el('span', {class: 'chip ' + (ok ? 'status-ok' : 'status-bad')},
          el('i', {class: 'dot', style: 'background:' + (ok ? 'var(--good)' : 'var(--crit)')}),
          ok ? 'Healthy' : 'Needs attention')),
      el('div', {class: 'mission', text: ok ? a.mission : a.problems.join('; ')}),
      bar, counts, foot);
    cards[a.name] = card;
    agentsBox.append(card);
  }

  // Filters
  const opt = (v, t) => el('option', {value: v, text: t});
  const fTeam = document.getElementById('f-team'), fAgent = document.getElementById('f-agent');
  const fPri = document.getElementById('f-priority'), fQ = document.getElementById('f-q');
  fTeam.append(opt('', 'All teams'), ...Object.entries(D.teams).map(([k, v]) => opt(k, v)));
  const agentNames = [...new Set([...D.agents.map(a => a.name), ...D.actions.map(a => a.agent)])];
  fAgent.append(opt('', 'All agents'), ...agentNames.map(n => opt(n, AGENT_LABEL(n))));
  fPri.append(opt('', 'Any priority'), ...['now', 'high', 'medium', 'low', 'judgment'].map(p => opt(p, p[0].toUpperCase() + p.slice(1))));
  const seg = document.getElementById('f-stage');
  const segBtns = [['', 'All'], ...STAGES.map(([k, l]) => [k, k === 'waiting' ? 'Waiting' : l])].map(([k, l]) => {
    const b = el('button', {type: 'button', 'aria-pressed': 'false', text: l, onclick: () => { state.stage = k; sync(); }});
    b.dataset.k = k; seg.append(b); return b;
  });
  fTeam.onchange = () => { state.team = fTeam.value; sync(); };
  fAgent.onchange = () => { state.agent = fAgent.value; sync(); };
  fPri.onchange = () => { state.priority = fPri.value; sync(); };
  fQ.oninput = () => { state.q = fQ.value.trim().toLowerCase(); sync(); };
  document.getElementById('f-clear').onclick = () => { Object.assign(state, {team: '', agent: '', stage: '', priority: '', q: ''}); fQ.value = ''; sync(); };
  if (D.token) document.getElementById('th-act').textContent = 'Do';

  // Actions (server mode)
  async function act(id, op, row) {
    row.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const r = await fetch('/api/action', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Tracker-Token': D.token},
        body: JSON.stringify({id, op})});
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || r.status);
      location.reload();
    } catch (e) {
      alert('Could not ' + op + ' #' + id + ': ' + e.message);
      row.querySelectorAll('button').forEach(b => b.disabled = false);
    }
  }

  // Table
  const tbody = document.getElementById('rows');
  function sync() {
    fTeam.value = state.team; fAgent.value = state.agent; fPri.value = state.priority;
    segBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.k === state.stage)));
    for (const [n, c] of Object.entries(cards)) {
      c.classList.toggle('selected', n === state.agent); c.setAttribute('aria-pressed', String(n === state.agent));
    }
    const rows = D.actions.filter(a =>
      (!state.team || a.team === state.team) && (!state.agent || a.agent === state.agent) &&
      (!state.stage || a.stage === state.stage) && (!state.priority || a.priority === state.priority) &&
      (!state.q || (a.title + ' ' + a.reason + ' ' + a.sku + ' ' + a.agent).toLowerCase().includes(state.q)))
      .sort((x, y) => (x.stage === 'waiting' ? 0 : 1) - (y.stage === 'waiting' ? 0 : 1)
        || PRI_ORDER[x.priority] - PRI_ORDER[y.priority] || (y.impact || 0) - (x.impact || 0) || y.id - x.id);
    tbody.replaceChildren();
    if (!rows.length) tbody.append(el('tr', {}, el('td', {colspan: '11', class: 'empty', text: 'Nothing matches these filters.'})));
    for (const a of rows) {
      const tr = el('tr');
      const btns = el('div', {class: 'btns'});
      if (D.token && a.status === 'pending_approval') {
        btns.append(el('button', {type: 'button', class: 'primary', text: 'Approve', onclick: () => act(a.id, 'approve', tr)}),
                    el('button', {type: 'button', text: 'Reject', onclick: () => act(a.id, 'reject', tr)}));
      } else if (D.token && a.status === 'acknowledged') {
        btns.append(el('button', {type: 'button', class: 'primary', text: 'Mark done', onclick: () => act(a.id, 'done', tr)}));
      }
      const why = a.reason + (a.notes ? ' (' + a.notes + ')' : '');
      const stale = a.stage === 'waiting' && a.age >= D.stale_days;
      tr.append(
        el('td', {class: 'num', text: a.id}),
        el('td', {}, el('div', {class: 'title', text: a.title}), el('div', {class: 'why', text: why})),
        el('td', {text: AGENT_LABEL(a.agent)}),
        el('td', {class: 'hide-sm', text: D.teams[a.team] || '—'}),
        el('td', {}, a.priority ? el('span', {class: 'pri pri-' + a.priority, text: a.priority}) : '—'),
        el('td', {class: 'num', text: usd(a.impact)}),
        el('td', {class: 'hide-sm', text: a.effort || '—'}),
        el('td', {class: 'num hide-sm', text: a.roi == null ? '—' : a.roi + '×'}),
        el('td', {}, el('span', {class: 'chip'}, el('i', {class: 'dot', style: 'background:' + STAGE_COLOR[a.stage]}),
          STATUS_LABEL[a.status] || a.status), a.verdict ? el('div', {class: 'why', text: 'Result: ' + a.verdict}) : null),
        el('td', {class: 'num' + (stale ? ' stale' : ''), text: a.age === 0 ? 'today' : a.age + 'd'}),
        el('td', {}, btns));
      tbody.append(tr);
    }
    const parts = [rows.length + ' of ' + D.actions.length + ' actions'];
    if (!D.token) parts.push('Read-only. To approve here: amz-agents dashboard --serve --allow-actions');
    document.getElementById('queue-note').textContent = parts.join(' · ');
  }
  sync();
})();
</script>
</body>
</html>
"""
