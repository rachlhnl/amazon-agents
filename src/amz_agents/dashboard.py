"""Agent work tracker: what needs you, a board of every agent's work, and whether
each agent is healthy.

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
<title>Agent work tracker</title>
<style>
:root {
  color-scheme: light;
  --bg: #f6f6f4; --surface: #fcfcfb; --surface-2: #f0efec; --border: #e2e1dc;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #75746f;
  --accent: #2a78d6; --accent-soft: #e6f0fc;
  --st-review: #86b6ef; --st-doing: #2a78d6; --st-done: #0ca30c; --st-stopped: #b9b8b2;
  --good: #0ca30c; --good-text: #006300; --good-soft: #e3f4e3;
  --warn: #fab219; --warn-text: #8a5a00; --warn-soft: #fdf1d6;
  --crit: #d03b3b; --crit-text: #b02a2a; --crit-soft: #fbe5e5;
  --shadow: 0 1px 2px rgba(0,0,0,.05);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
    --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
    --accent: #3987e5; --accent-soft: #1d2a3a;
    --st-review: #6da7ec; --st-doing: #3987e5; --st-done: #0ca30c; --st-stopped: #5e5d58;
    --good-text: #4cc24c; --good-soft: #17301a; --warn-text: #fab219; --warn-soft: #33290f;
    --crit-text: #e66767; --crit-soft: #3a1c1c; --shadow: none;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #121211; --surface: #1a1a19; --surface-2: #232321; --border: #383835;
  --text: #f0efec; --text-2: #c3c2b7; --text-3: #9a998f;
  --accent: #3987e5; --accent-soft: #1d2a3a;
  --st-review: #6da7ec; --st-doing: #3987e5; --st-done: #0ca30c; --st-stopped: #5e5d58;
  --good-text: #4cc24c; --good-soft: #17301a; --warn-text: #fab219; --warn-soft: #33290f;
  --crit-text: #e66767; --crit-soft: #3a1c1c; --shadow: none;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button { font: inherit; color: inherit; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 24px 16px 48px; }
header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: flex-start; gap: 12px; }
h1 { font-size: 22px; margin: 0; font-weight: 650; }
h2 { font-size: 16px; margin: 0; font-weight: 650; }
.meta { color: var(--text-2); font-size: 13px; display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 4px; }
.theme { border: 1px solid var(--border); background: var(--surface); border-radius: 8px; padding: 6px 10px;
  font-size: 13px; cursor: pointer; color: var(--text-2); }
.headline { font-size: 18px; margin: 20px 0 16px; max-width: 900px; }
.headline b { font-weight: 650; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px;
  background: var(--surface-2); color: var(--text-2); font-size: 12px; white-space: nowrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px;
  box-shadow: var(--shadow); }
.sec-head { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 6px 12px;
  margin: 28px 0 12px; }
.sec-head p { margin: 0; color: var(--text-3); font-size: 13px; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px;
  text-align: left; cursor: pointer; box-shadow: var(--shadow); }
.tile:hover, .tile:focus-visible { border-color: var(--accent); outline: none; }
.tile .label { color: var(--text-2); font-size: 13px; display: flex; align-items: center; gap: 6px; }
.tile .value { font-size: 28px; font-weight: 650; margin-top: 2px; font-variant-numeric: tabular-nums; }
.tile .sub { color: var(--text-3); font-size: 12px; }
.tile.alert .value { color: var(--crit-text); }
/* Inbox */
.inbox { display: grid; gap: 8px; }
.todo { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 12px; align-items: center;
  background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--st-review);
  border-radius: 10px; padding: 12px 14px; box-shadow: var(--shadow); }
.todo.p-now { border-left-color: var(--crit); }
.todo.p-high { border-left-color: var(--accent); }
.todo.health { border-left-color: var(--warn); }
.todo .t { font-weight: 600; cursor: pointer; background: none; border: 0; padding: 0; text-align: left; }
.todo .t:hover { color: var(--accent); }
.todo .s { color: var(--text-2); font-size: 12.5px; grid-column: 2; }
.todo .acts { grid-row: 1 / span 2; grid-column: 3; display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
.avatar { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; font-size: 12px;
  font-weight: 700; background: var(--accent-soft); color: var(--accent); flex: none; grid-row: 1 / span 2; }
.todo.health .avatar { background: var(--warn-soft); color: var(--warn-text); }
.more { color: var(--text-3); font-size: 13px; padding: 4px 2px; }
.caught-up { color: var(--good-text); background: var(--good-soft); border-radius: 10px; padding: 12px 14px; font-weight: 550; }
/* Filters */
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.pill { border: 1px solid var(--border); background: var(--surface); border-radius: 999px; padding: 4px 12px;
  font-size: 13px; color: var(--text-2); cursor: pointer; }
.pill[aria-pressed=true] { background: var(--accent-soft); border-color: var(--accent); color: var(--text); font-weight: 600; }
select, input[type=search] { font: inherit; font-size: 13px; color: var(--text); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; }
input[type=search] { min-width: 180px; flex: 1; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { font-size: 13px; padding: 6px 12px; border: 0; background: var(--surface); color: var(--text-2); cursor: pointer; }
.seg button + button { border-left: 1px solid var(--border); }
.seg button[aria-pressed=true] { background: var(--accent-soft); color: var(--text); font-weight: 600; }
.clear { font-size: 13px; background: none; border: 0; color: var(--accent); cursor: pointer; }
/* Board */
.board { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; align-items: start; }
.col { background: var(--surface-2); border-radius: 12px; padding: 10px; min-width: 0; }
.col-head { display: flex; align-items: center; gap: 8px; padding: 2px 4px 10px; font-weight: 650; }
.col-head .n { margin-left: auto; color: var(--text-3); font-weight: 500; font-variant-numeric: tabular-nums; }
.col-head .hint { display: block; font-weight: 400; color: var(--text-3); font-size: 12px; }
.col-body { display: grid; gap: 8px; }
.item { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
  cursor: pointer; text-align: left; width: 100%; box-shadow: var(--shadow); }
.item:hover, .item:focus-visible { border-color: var(--accent); outline: none; }
.dtags { display: flex; flex-wrap: wrap; gap: 4px; }
.item .tags { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 6px; }
.item .t { font-weight: 550; font-size: 13.5px; overflow-wrap: anywhere; }
.item .who { display: flex; justify-content: space-between; gap: 8px; color: var(--text-3); font-size: 12px; margin-top: 8px; }
.item .who .imp { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 550; }
.tag { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-2); white-space: nowrap; }
.tag.now { border-color: var(--crit); color: var(--crit-text); font-weight: 600; }
.tag.high { border-color: var(--accent); color: var(--accent); font-weight: 600; }
.tag.ok { background: var(--accent); border-color: var(--accent); color: #fff; }
.tag.you { background: var(--warn-soft); border-color: var(--warn); color: var(--warn-text); }
.tag.bad { background: var(--crit-soft); border-color: var(--crit); color: var(--crit-text); }
.tag.good { background: var(--good-soft); border-color: var(--good); color: var(--good-text); }
.stale { color: var(--warn-text); font-weight: 600; }
.col-more { font-size: 12.5px; color: var(--accent); background: none; border: 0; cursor: pointer; padding: 4px; }
/* List */
.table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 600; color: var(--text-2); padding: 10px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
tbody tr { cursor: pointer; } tbody tr:hover { background: var(--surface-2); }
tr:last-child td { border-bottom: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
/* Team */
.team { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.agent { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; align-items: center; text-align: left;
  background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; cursor: pointer;
  box-shadow: var(--shadow); }
.agent:hover, .agent:focus-visible { border-color: var(--accent); outline: none; }
.agent.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.agent .nm { font-weight: 650; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.agent .sub { color: var(--text-3); font-size: 12px; }
.agent .line { grid-column: 1 / -1; color: var(--text-2); font-size: 12.5px; margin-top: 8px; }
.agent .line.bad { color: var(--crit-text); }
.bar { grid-column: 1 / -1; display: flex; height: 8px; border-radius: 4px; overflow: hidden; background: var(--surface-2); gap: 2px; margin-top: 8px; }
.bar div { height: 100%; min-width: 3px; }
.counts { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 2px 12px; font-size: 12px; color: var(--text-2); margin-top: 6px; }
.counts b { color: var(--text); font-variant-numeric: tabular-nums; }
.health-ok { color: var(--good-text); } .health-bad { color: var(--crit-text); }
/* Buttons */
.btn { font-size: 12.5px; padding: 5px 12px; border-radius: 7px; cursor: pointer; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); white-space: nowrap; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.btn:disabled { opacity: .5; cursor: default; }
/* Drawer */
.scrim { position: fixed; inset: 0; background: rgba(0,0,0,.25); display: none; z-index: 20; }
.drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(460px, 100%); background: var(--surface);
  border-left: 1px solid var(--border); z-index: 21; transform: translateX(100%); transition: transform .18s ease;
  overflow-y: auto; padding: 20px; }
.open .scrim { display: block; } .open .drawer { transform: none; }
.drawer .x { float: right; background: none; border: 0; font-size: 22px; line-height: 1; cursor: pointer; color: var(--text-2); }
.drawer h3 { font-size: 17px; margin: 8px 30px 12px 0; overflow-wrap: anywhere; }
.steps { display: flex; gap: 4px; margin: 4px 0 16px; }
.steps div { flex: 1; font-size: 11px; color: var(--text-3); border-top: 3px solid var(--surface-2); padding-top: 4px; }
.steps div.on { border-top-color: var(--accent); color: var(--text); font-weight: 600; }
.steps div.stop { border-top-color: var(--crit); color: var(--crit-text); font-weight: 600; }
dl { display: grid; grid-template-columns: 110px 1fr; gap: 8px 12px; margin: 0 0 16px; font-size: 13px; }
dt { color: var(--text-3); } dd { margin: 0; overflow-wrap: anywhere; }
.why-box { background: var(--surface-2); border-radius: 10px; padding: 12px; font-size: 13px; margin-bottom: 16px; white-space: pre-wrap; overflow-wrap: anywhere; }
.drawer .acts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.note { color: var(--text-3); font-size: 12px; margin-top: 10px; }
.msg { font-size: 12.5px; color: var(--crit-text); }
.empty { padding: 20px; text-align: center; color: var(--text-3); font-size: 13px; }
#tip { position: fixed; pointer-events: none; background: var(--text); color: var(--bg); font-size: 12px;
  padding: 6px 8px; border-radius: 6px; display: none; z-index: 30; white-space: nowrap; }
@media (max-width: 900px) { .board { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) {
  .board { grid-template-columns: 1fr; }
  .todo { grid-template-columns: auto 1fr; }
  .todo .acts { grid-row: auto; grid-column: 1 / -1; justify-content: flex-start; }
  .hide-sm { display: none; } input[type=search] { min-width: 100%; }
  dl { grid-template-columns: 90px 1fr; }
}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Agent work tracker</h1>
      <div class="meta" id="meta"></div>
    </div>
    <button class="theme" id="theme" type="button">Dark / light</button>
  </header>
  <p class="headline" id="headline"></p>
  <section class="kpis" id="kpis" aria-label="Summary"></section>

  <div class="sec-head"><h2>Needs you</h2><p id="inbox-note"></p></div>
  <section class="inbox" id="inbox" aria-label="Needs you"></section>

  <div class="sec-head"><h2>All work</h2><p id="work-note"></p></div>
  <div class="filters">
    <div class="seg" role="group" aria-label="View">
      <button type="button" id="v-board" aria-pressed="true">Board</button>
      <button type="button" id="v-list" aria-pressed="false">List</button>
    </div>
    <span id="f-teams" role="group" aria-label="Team" style="display:contents"></span>
    <select id="f-agent" aria-label="Agent"></select>
    <input type="search" id="f-q" placeholder="Search work" aria-label="Search work">
    <button class="clear" id="f-clear" type="button">Clear</button>
  </div>
  <section id="board" class="board" aria-label="Board"></section>
  <section id="list" class="table-wrap" hidden>
    <table>
      <thead><tr><th>Work</th><th>Agent</th><th>Stage</th><th class="hide-sm">Priority</th>
        <th class="num">Est. impact</th><th class="num">Age</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </section>

  <div class="sec-head"><h2>The team</h2><p>Click an agent to see only their work.</p></div>
  <section class="team" id="team" aria-label="Agents"></section>
  <p class="note" id="foot"></p>
</div>
<div id="shell">
  <div class="scrim" id="scrim"></div>
  <aside class="drawer" id="drawer" aria-label="Details" aria-hidden="true"></aside>
</div>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>
(() => {
  const D = JSON.parse(document.getElementById('data').textContent);
  // Board columns, in the order work moves through them.
  const COLS = [
    ['review', 'To review', 'Suggested by an agent, or waiting for your OK', 'var(--st-review)'],
    ['doing', 'Being done', 'Approved; a person or the API is on it', 'var(--st-doing)'],
    ['done', 'Done', 'Finished in the last ' + D.window_days + ' days', 'var(--st-done)'],
    ['stopped', 'Stopped', 'Rejected or failed', 'var(--st-stopped)'],
  ];
  const COL_OF = {waiting: 'review', in_progress: 'doing', done: 'done', rejected: 'stopped', failed: 'stopped'};
  const COL_LABEL = Object.fromEntries(COLS.map(c => [c[0], c[1]]));
  const COL_COLOR = Object.fromEntries(COLS.map(c => [c[0], c[3]]));
  const STATUS = {
    proposed: ['Suggestion', ''], pending_approval: ['Needs your OK', 'ok'],
    approved: ['Approved', ''], acknowledged: ['Your team to do', 'you'],
    executed: ['Done by API', 'good'], done: ['Done', 'good'],
    rejected: ['Rejected', ''], failed: ['Failed', 'bad'],
  };
  const PRI = {now: 0, high: 1, medium: 2, low: 3, judgment: 4, '': 5};
  const PRI_LABEL = {now: 'Urgent', high: 'High', medium: 'Medium', low: 'Low', judgment: 'Your call'};
  const EFFORT_LABEL = {XS: 'Minutes', S: 'Under an hour', M: 'A couple of hours', L: 'About a day', XL: 'Several days'};
  const NAME = n => n === 'chief_of_staff' ? 'Chief of staff' : n === 'ux' ? 'UX' : n[0].toUpperCase() + n.slice(1).replace(/_/g, ' ');
  const INITIALS = n => n === 'chief_of_staff' ? 'CS' : n === 'ux' ? 'UX' : n.slice(0, 2).toUpperCase();
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null) continue;
      if (k === 'class') e.className = v; else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v);
    }
    kids.flat().forEach(k => k != null && k !== false && e.append(k));
    return e;
  };
  const usd = v => v == null ? '—' : (v < 0 ? '−$' : '$') + Math.abs(Math.round(v)).toLocaleString();
  const fmt = n => n.toLocaleString();
  const age = d => d === 0 ? 'today' : d === 1 ? '1 day' : d + ' days';
  const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

  // Theme toggle
  const root = document.documentElement;
  try { const t = localStorage.getItem('tracker-theme'); if (t) root.dataset.theme = t; } catch (e) {}
  document.getElementById('theme').onclick = () => {
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('tracker-theme', root.dataset.theme); } catch (e) {}
  };

  const tip = document.getElementById('tip');
  const showTip = (e, text) => { tip.textContent = text; tip.style.display = 'block';
    tip.style.left = Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8) + 'px';
    tip.style.top = (e.clientY + 14) + 'px'; };
  const hideTip = () => { tip.style.display = 'none'; };

  for (const a of D.actions) a.col = COL_OF[a.stage] || 'review';
  const byPri = (x, y) => PRI[x.priority] - PRI[y.priority] || (y.impact || 0) - (x.impact || 0) || y.age - x.age;
  const needsOk = a => a.status === 'pending_approval';
  const needsDone = a => a.status === 'acknowledged';
  // Suggestions only reach the inbox when they're urgent or a judgment call.
  const isTodo = a => needsOk(a) || needsDone(a) || a.status === 'failed'
    || (a.status === 'proposed' && (a.priority === 'now' || a.priority === 'high'));

  // Header
  const meta = document.getElementById('meta');
  if (D.run) meta.append(el('span', {text: 'Last run ' + D.run.finished + ' UTC'}),
    el('span', {class: 'chip', text: {recommend: 'Suggest only', approve: 'Approve first', auto: 'Auto within limits'}[D.run.mode] || D.run.mode}));
  else meta.append(el('span', {text: 'The agents haven’t run yet'}));
  meta.append(el('span', {text: 'Sales data through ' + (D.data_through || 'n/a')}));

  // Headline sentence
  const K = D.kpis;
  // Work only a person can move goes first, then the best ideas.
  const mustAct = a => needsOk(a) || needsDone(a) || a.status === 'failed' ? 0 : 1;
  const todos = D.actions.filter(isTodo).sort((x, y) => mustAct(x) - mustAct(y) || byPri(x, y));
  const sick = D.agents.filter(a => a.problems.length);
  const hl = document.getElementById('headline');
  if (!D.run) {
    hl.append('Nothing to track yet. Run ', el('b', {text: 'amz-agents run'}),
      ' (or ', el('b', {text: 'amz-agents demo'}), ' for sample data) and reload this page.');
  } else {
    const parts = [];
    if (todos.length) parts.push([el('b', {text: plural(todos.length, 'item')}), ' need you']);
    if (K.in_progress) parts.push([el('b', {text: fmt(K.in_progress)}), ' being done']);
    parts.push([el('b', {text: fmt(K.done)}), ' finished in ' + D.window_days + ' days']);
    parts.forEach((p, i) => { if (i) hl.append(i === parts.length - 1 ? ' and ' : ', '); hl.append(...p); });
    hl.append('.');
    if (K.waiting_impact > 0) hl.append(' Open suggestions are worth about ', el('b', {text: usd(K.waiting_impact)}), ' over 30 days.');
    if (sick.length) hl.append(' ', el('b', {text: plural(sick.length, 'agent')}), sick.length === 1 ? ' needs' : ' need', ' attention.');
  }

  // State and filters
  const state = {view: 'board', team: '', agent: '', col: '', q: ''};
  try { const v = localStorage.getItem('tracker-view'); if (v === 'list' || v === 'board') state.view = v; } catch (e) {}
  const focus = sel => { const t = document.querySelector(sel); if (t) t.scrollIntoView({behavior: 'smooth', block: 'start'}); };

  // KPI tiles: each one filters the board.
  const tile = (label, value, sub, color, onclick, alert) => el('button', {type: 'button', class: 'tile' + (alert ? ' alert' : ''), onclick},
    el('div', {class: 'label'}, color ? el('i', {class: 'dot', style: 'background:' + color}) : null, label),
    el('div', {class: 'value', text: value}), sub ? el('div', {class: 'sub', text: sub}) : null);
  const show = col => () => { Object.assign(state, {col, team: '', agent: '', q: ''}); fQ.value = ''; sync(); focus('#f-q'); };
  document.getElementById('kpis').append(
    tile('Needs you', fmt(todos.length), todos.length ? 'approvals, to-dos and urgent ideas' : 'all caught up', null,
      () => focus('#inbox'), todos.some(a => a.priority === 'now')),
    tile('To review', fmt(K.waiting), usd(K.waiting_impact) + ' est. 30-day value', 'var(--st-review)', show('review')),
    tile('Being done', fmt(K.in_progress), 'approved, in motion', 'var(--st-doing)', show('doing')),
    tile('Done', fmt(K.done), 'last ' + D.window_days + ' days', 'var(--st-done)', show('done')),
    tile('Agents healthy', K.healthy + ' / ' + K.agents, K.healthy < K.agents ? 'some are missing data' : 'all running', null,
      () => focus('#team'), K.healthy < K.agents));

  // Actions against the server (only when it was started with --allow-actions)
  async function act(a, op, btn) {
    const box = btn.parentElement;
    box.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const r = await fetch('/api/action', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Tracker-Token': D.token},
        body: JSON.stringify({id: a.id, op})});
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || r.status);
      location.reload();
    } catch (e) {
      box.querySelectorAll('button').forEach(b => b.disabled = false);
      box.append(el('span', {class: 'msg', text: 'Could not ' + op + ': ' + e.message}));
    }
  }
  const buttons = a => {
    if (!D.token) return [];
    if (needsOk(a)) return [el('button', {type: 'button', class: 'btn primary', text: 'Approve', onclick: e => { e.stopPropagation(); act(a, 'approve', e.target); }}),
      el('button', {type: 'button', class: 'btn', text: 'Reject', onclick: e => { e.stopPropagation(); act(a, 'reject', e.target); }})];
    if (needsDone(a)) return [el('button', {type: 'button', class: 'btn primary', text: 'Mark done', onclick: e => { e.stopPropagation(); act(a, 'done', e.target); }})];
    return [];
  };
  const tags = a => {
    const [label, cls] = STATUS[a.status] || [a.status, ''];
    return [
      a.priority && a.priority !== 'medium' && a.priority !== 'low' ? el('span', {class: 'tag ' + a.priority, text: PRI_LABEL[a.priority]}) : null,
      el('span', {class: 'tag ' + cls, text: label}),
      a.verdict ? el('span', {class: 'tag ' + (a.verdict === 'helped' ? 'good' : a.verdict === 'hurt' ? 'bad' : ''), text: 'Result: ' + a.verdict}) : null,
    ];
  };

  // Details drawer
  const shell = document.getElementById('shell'), drawer = document.getElementById('drawer');
  let lastFocus = null;
  const close = () => { shell.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); if (lastFocus) lastFocus.focus(); };
  document.getElementById('scrim').onclick = close;
  addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  function open(a) {
    lastFocus = document.activeElement;
    const order = ['review', 'doing', 'done'];
    const steps = el('div', {class: 'steps'}, ...order.map((c, i) => el('div', {
      class: a.col === 'stopped' ? (i === 0 ? 'stop' : '') : (order.indexOf(a.col) >= i ? 'on' : ''),
      text: a.col === 'stopped' && i === 0 ? (a.status === 'failed' ? 'Failed' : 'Rejected') : COL_LABEL[c]})));
    const row = (k, v) => v == null || v === '' ? [] : [el('dt', {text: k}), el('dd', {text: v})];
    const acts = buttons(a);
    let help = null;
    if (!D.token && (needsOk(a) || needsDone(a))) help = el('p', {class: 'note', text: 'To approve or finish this here, start the tracker with: amz-agents dashboard --serve --allow-actions'});
    else if (a.status === 'proposed') help = el('p', {class: 'note', text: 'Agents are in suggest-only mode, so this is advice: act on it yourself, or switch AGENT_MODE to approve so suggestions queue for your OK.'});
    drawer.replaceChildren(...[
      el('button', {type: 'button', class: 'x', 'aria-label': 'Close', text: '×', onclick: close}),
      el('div', {class: 'dtags'}, tags(a)),
      el('h3', {text: a.title}), steps,
      el('div', {class: 'why-box', text: (a.reason || 'No reason recorded.') + (a.notes ? '\n\n' + a.notes : '')}),
      acts.length ? el('div', {class: 'acts'}, acts) : null,
      el('dl', {}, row('Agent', NAME(a.agent)), row('Team', D.teams[a.team] || ''), row('Est. impact', a.impact == null ? '' : usd(a.impact) + ' over 30 days'),
        row('Effort', a.effort ? EFFORT_LABEL[a.effort] || a.effort : ''), row('Return', a.roi == null ? '' : a.roi + '× the time it takes'),
        row('Product', a.sku), row('Raised', a.created + ' (' + age(a.age) + (a.age ? ' ago' : '') + ')'), row('Item', '#' + a.id + ' · ' + a.kind.replace(/_/g, ' '))),
      help].filter(Boolean));
    shell.classList.add('open'); drawer.setAttribute('aria-hidden', 'false');
    drawer.querySelector('.x').focus();
  }

  // Inbox: what needs a person now, plus agents that are missing data.
  const INBOX_MAX = 6;
  const inbox = document.getElementById('inbox');
  function renderInbox() {
    inbox.replaceChildren();
    if (!todos.length && !sick.length) { inbox.append(el('div', {class: 'caught-up', text: D.run ? 'You’re all caught up. Nothing needs you right now.' : 'Nothing yet.'})); return; }
    for (const a of todos.slice(0, INBOX_MAX)) {
      const what = needsOk(a) ? 'Approve or reject' : needsDone(a) ? 'Do it, then mark done' : a.status === 'failed' ? 'Failed: take a look' : 'Worth acting on';
      inbox.append(el('div', {class: 'todo p-' + (a.priority || 'none')},
        el('span', {class: 'avatar', title: NAME(a.agent), text: INITIALS(a.agent)}),
        el('button', {type: 'button', class: 't', text: a.title, onclick: () => open(a)}),
        el('div', {class: 'acts'}, buttons(a).length ? buttons(a) : el('button', {type: 'button', class: 'btn', text: 'Details', onclick: () => open(a)})),
        el('div', {class: 's'}, what + ' · ' + NAME(a.agent) + (a.impact > 0 ? ' · ' + usd(a.impact) : '') + ' · ',
          el('span', {class: a.age >= D.stale_days ? 'stale' : '', text: a.age ? 'waiting ' + age(a.age) : 'new today'}))));
    }
    if (todos.length > INBOX_MAX) inbox.append(el('div', {class: 'more', text: '+ ' + (todos.length - INBOX_MAX) + ' more in To review below'}));
    for (const g of sick) inbox.append(el('div', {class: 'todo health'},
      el('span', {class: 'avatar', text: INITIALS(g.name)}),
      el('button', {type: 'button', class: 't', text: NAME(g.name) + ' agent can’t do its job', onclick: () => { state.agent = g.name; sync(); focus('#f-q'); }}),
      el('div', {class: 'acts'}),
      el('div', {class: 's', text: g.problems.join('; ')})));
  }
  renderInbox();
  document.getElementById('inbox-note').textContent = D.token ? 'Approve, reject or mark done right here.' : 'Click any item for details.';

  // Filters
  const teamBox = document.getElementById('f-teams');
  const teamBtns = [['', 'All teams'], ...Object.entries(D.teams)].map(([k, v]) => {
    const b = el('button', {type: 'button', class: 'pill', text: v, onclick: () => { state.team = state.team === k ? '' : k; sync(); }});
    b.dataset.k = k; teamBox.append(b); return b;
  });
  const fAgent = document.getElementById('f-agent'), fQ = document.getElementById('f-q');
  const agentNames = [...new Set([...D.agents.map(a => a.name), ...D.actions.map(a => a.agent)])];
  fAgent.append(el('option', {value: '', text: 'All agents'}), ...agentNames.map(n => el('option', {value: n, text: NAME(n)})));
  fAgent.onchange = () => { state.agent = fAgent.value; sync(); };
  fQ.oninput = () => { state.q = fQ.value.trim().toLowerCase(); sync(); };
  document.getElementById('f-clear').onclick = () => { Object.assign(state, {team: '', agent: '', col: '', q: ''}); fQ.value = ''; sync(); };
  const vBoard = document.getElementById('v-board'), vList = document.getElementById('v-list');
  const setView = v => { state.view = v; try { localStorage.setItem('tracker-view', v); } catch (e) {} sync(); };
  vBoard.onclick = () => setView('board'); vList.onclick = () => setView('list');

  // Team cards
  const teamEl = document.getElementById('team');
  const agentCards = {};
  for (const g of D.agents) {
    const st = {review: g.stages.waiting || 0, doing: g.stages.in_progress || 0, done: g.stages.done || 0,
      stopped: (g.stages.rejected || 0) + (g.stages.failed || 0)};
    const total = Object.values(st).reduce((x, y) => x + y, 0);
    const bar = el('div', {class: 'bar', role: 'img', 'aria-label': COLS.filter(c => st[c[0]]).map(c => c[1] + ' ' + st[c[0]]).join(', ') || 'No work yet'});
    for (const [k, label, , color] of COLS) {
      if (!st[k]) continue;
      const s = el('div', {style: 'flex:' + st[k] + ';background:' + color});
      s.addEventListener('mousemove', e => showTip(e, label + ': ' + st[k] + ' of ' + total));
      s.addEventListener('mouseleave', hideTip);
      bar.append(s);
    }
    const ok = !g.problems.length;
    let line = ok ? g.mission : g.problems.join('; ');
    if (ok && g.oldest_waiting != null && g.oldest_waiting >= D.stale_days) line = 'Oldest item has waited ' + age(g.oldest_waiting) + '.';
    const card = el('button', {type: 'button', class: 'agent', 'aria-pressed': 'false',
      onclick: () => { state.agent = state.agent === g.name ? '' : g.name; sync(); focus('#f-q'); }},
      el('span', {class: 'avatar', style: 'grid-row:1 / span 2', text: INITIALS(g.name)}),
      el('div', {class: 'nm'}, NAME(g.name),
        el('span', {class: 'chip ' + (ok ? 'health-ok' : 'health-bad')}, el('i', {class: 'dot', style: 'background:' + (ok ? 'var(--good)' : 'var(--crit)')}), ok ? 'Healthy' : 'Needs attention')),
      el('div', {class: 'sub', text: g.team_label + ' · ' + plural(g.proposed_last_run, 'idea') + ' last run'}),
      el('div', {class: 'line' + (ok ? '' : ' bad'), text: line}),
      total ? bar : null,
      el('div', {class: 'counts'}, total ? COLS.filter(c => st[c[0]]).map(c => el('span', {}, el('b', {text: st[c[0]]}), ' ' + c[1].toLowerCase()))
        : el('span', {text: 'No work yet'}),
        g.helped || g.hurt ? el('span', {text: g.helped + ' helped · ' + g.hurt + ' hurt'}) : null));
    agentCards[g.name] = card;
    teamEl.append(card);
  }

  // Board and list
  const COL_MAX = 8;
  const expanded = {};
  const board = document.getElementById('board'), list = document.getElementById('list'), tbody = document.getElementById('rows');
  function card(a) {
    return el('button', {type: 'button', class: 'item', onclick: () => open(a)},
      el('div', {class: 'tags'}, tags(a)),
      el('div', {class: 't', text: a.title}),
      el('div', {class: 'who'}, el('span', {text: NAME(a.agent) + ' · ' + (a.col === 'review' ? (a.age ? 'waiting ' + age(a.age) : 'new') : age(a.age) + (a.age ? ' ago' : ''))}),
        a.impact > 0 ? el('span', {class: 'imp', text: usd(a.impact)}) : null));
  }
  function sync() {
    fAgent.value = state.agent; vBoard.setAttribute('aria-pressed', String(state.view === 'board')); vList.setAttribute('aria-pressed', String(state.view === 'list'));
    teamBtns.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.k === state.team)));
    for (const [n, c] of Object.entries(agentCards)) { c.classList.toggle('selected', n === state.agent); c.setAttribute('aria-pressed', String(n === state.agent)); }
    const rows = D.actions.filter(a => (!state.team || a.team === state.team) && (!state.agent || a.agent === state.agent)
      && (!state.col || a.col === state.col)
      && (!state.q || (a.title + ' ' + a.reason + ' ' + a.sku + ' ' + NAME(a.agent)).toLowerCase().includes(state.q)));
    board.hidden = state.view !== 'board'; list.hidden = state.view !== 'list';
    board.replaceChildren();
    for (const [k, label, hint, color] of COLS) {
      if (state.col && state.col !== k) continue;
      const mine = rows.filter(a => a.col === k).sort(k === 'review' ? byPri : (x, y) => x.age - y.age || y.id - x.id);
      const body = el('div', {class: 'col-body'});
      const cap = expanded[k] || state.col ? mine.length : COL_MAX;
      mine.slice(0, cap).forEach(a => body.append(card(a)));
      if (!mine.length) body.append(el('div', {class: 'empty', text: 'Nothing here'}));
      if (mine.length > cap) body.append(el('button', {type: 'button', class: 'col-more', text: 'Show ' + (mine.length - cap) + ' more', onclick: () => { expanded[k] = true; sync(); }}));
      board.append(el('div', {class: 'col'},
        el('div', {class: 'col-head'}, el('i', {class: 'dot', style: 'background:' + color}),
          el('span', {}, label, el('span', {class: 'hint', text: hint})), el('span', {class: 'n', text: mine.length})), body));
    }
    board.style.gridTemplateColumns = state.col ? '1fr' : '';
    tbody.replaceChildren();
    const sorted = [...rows].sort((x, y) => COLS.findIndex(c => c[0] === x.col) - COLS.findIndex(c => c[0] === y.col) || byPri(x, y));
    if (!sorted.length) tbody.append(el('tr', {}, el('td', {colspan: '6', class: 'empty', text: 'Nothing matches these filters.'})));
    for (const a of sorted) {
      const [label] = STATUS[a.status] || [a.status];
      tbody.append(el('tr', {onclick: () => open(a)},
        el('td', {}, el('div', {style: 'font-weight:550', text: a.title})),
        el('td', {text: NAME(a.agent)}),
        el('td', {}, el('span', {class: 'chip'}, el('i', {class: 'dot', style: 'background:' + COL_COLOR[a.col]}), label)),
        el('td', {class: 'hide-sm', text: PRI_LABEL[a.priority] || '—'}),
        el('td', {class: 'num', text: usd(a.impact)}),
        el('td', {class: 'num' + (a.col === 'review' && a.age >= D.stale_days ? ' stale' : ''), text: age(a.age)})));
    }
    const filtered = state.team || state.agent || state.col || state.q;
    document.getElementById('work-note').textContent = (filtered ? rows.length + ' of ' : '') + plural(D.actions.length, 'item') + ' · click any card for details';
  }
  sync();
  document.getElementById('foot').textContent = 'Covers the last ' + D.window_days + ' days plus anything still open. '
    + (D.token ? 'Approve / reject / mark done are on.' : 'Read-only. To approve here: amz-agents dashboard --serve --allow-actions');
})();
</script>
</body>
</html>
"""
