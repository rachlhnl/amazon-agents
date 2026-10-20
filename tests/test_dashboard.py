import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from amz_agents import dashboard
from amz_agents.actions import ActionLog
from amz_agents.config import Mode
from amz_agents.orchestrator import Orchestrator


def run(ctx, mode=Mode.RECOMMEND):
    log = ActionLog(ctx.wh, mode)
    Orchestrator(ctx, log).run()
    return log


def test_collect_before_any_run(ctx):
    data = dashboard.collect(ctx.wh)
    assert data["run"] is None
    assert all(a["problems"] == ["hasn't run yet"] for a in data["agents"])


def test_collect_tracks_every_agent_and_stage(ctx):
    run(ctx)
    data = dashboard.collect(ctx.wh)
    agents = {a["name"]: a for a in data["agents"]}
    assert len(agents) == 10 and data["kpis"]["healthy"] == 10
    assert data["run"]["mode"] == "recommend"
    assert agents["ux"]["stages"]["done"] == 1  # seeded page-speed fix
    assert agents["listing"]["stages"]["in_progress"] == 1
    assert agents["ads"]["stages"]["rejected"] == 1
    assert agents["marketing"]["stages"]["failed"] == 1
    assert agents["ads"]["helped"] == 1  # seeded bid cut judged "helped"
    assert data["kpis"]["failed"] == 1 and data["kpis"]["waiting"] > 0
    titles = {a["title"] for a in data["actions"]}
    assert "Reorder 1,214 × BTL-32-SGE" in titles


def test_agent_problems_reach_the_tracker(ctx):
    ctx.wh.execute("DELETE FROM external_campaigns")
    run(ctx)
    m = next(a for a in dashboard.collect(ctx.wh)["agents"] if a["name"] == "marketing")
    assert "no data in external_campaigns" in m["problems"]


def test_render_escapes_script_breakouts(ctx):
    run(ctx)
    ctx.wh.execute("UPDATE actions SET reason='</script><script>alert(1)</script>' WHERE id=3")
    html = dashboard.render(dashboard.collect(ctx.wh))
    assert "</script><script>alert(1)" not in html
    assert html.count("</script>") == 2  # the data block and the app script


def test_mark_human_work_done(ctx):
    log = ActionLog(ctx.wh, Mode.APPROVE)
    i = ctx.wh.query("SELECT id FROM actions WHERE status='acknowledged'")[0]["id"]
    log.complete(i)
    row = ctx.wh.query("SELECT status, done_at FROM actions WHERE id=?", (i,))[0]
    assert row["status"] == "done" and row["done_at"]
    with pytest.raises(ValueError):
        log.complete(i)


def _server(ctx, allow):
    log = run(ctx, Mode.APPROVE)
    token = "t0k" if allow else None
    real = dashboard.secrets.token_urlsafe
    dashboard.secrets.token_urlsafe = lambda n: token
    started = {}

    def fake_serve_forever(self):
        started["srv"] = self
        threading.Thread(target=HTTPServer.serve_forever, args=(self,), daemon=True).start()

    orig = HTTPServer.serve_forever
    HTTPServer.serve_forever = fake_serve_forever
    try:
        dashboard.serve(ctx.wh, log, "127.0.0.1", 0, allow)
    finally:
        HTTPServer.serve_forever = orig
        dashboard.secrets.token_urlsafe = real
    srv = started["srv"]
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", token


def _post(url, body, token):
    req = urllib.request.Request(url + "/api/action", data=json.dumps(body).encode(),
                                 headers={"X-Tracker-Token": token or "", "Content-Type": "application/json"})
    return urllib.request.urlopen(req)


def test_server_read_only_refuses_changes(ctx):
    srv, url, _ = _server(ctx, allow=False)
    try:
        assert "Agent work tracker" in urllib.request.urlopen(url).read().decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(url, {"id": 3, "op": "reject"}, "guess")
        assert e.value.code == 403
    finally:
        srv.shutdown()


def test_server_actions_need_the_token(ctx):
    srv, url, token = _server(ctx, allow=True)
    try:
        page = urllib.request.urlopen(url).read().decode()
        assert '"token": "t0k"' in page
        pending = ctx.wh.query("SELECT id FROM actions WHERE status='pending_approval' "
                               "AND kind='flag_defect'")[0]["id"]
        with pytest.raises(urllib.error.HTTPError):
            _post(url, {"id": pending, "op": "reject"}, "wrong")
        r = json.loads(_post(url, {"id": pending, "op": "reject"}, token).read())
        assert r["status"] == "rejected"
    finally:
        srv.shutdown()
