"""Command line entry point.

  amz-agents demo                 seed sample data and run all agents
  amz-agents ingest [--only JOBS] [--costs FILE]
                                  pull Amazon, Shopify and Meta data into the warehouse
  amz-agents import TABLE FILE    load a CSV export (reviews, leads, Google Ads, ...)
  amz-agents run [--no-email]     run all agents, write the brief and team briefs, email
                                  them if SMTP is set
  amz-agents actions [--status S] list logged actions
  amz-agents approve ID [ID ...]  approve and execute pending actions
  amz-agents reject ID [ID ...]   reject pending actions
  amz-agents done ID [ID ...]     mark approved human-only work as done
  amz-agents dashboard [--serve [--allow-actions] [--port N]] [--out FILE]
                                  the agent tracker (also written by run/demo)
  amz-agents market [--no-collect] [--out FILE]
                                  collect public data on the brands in market/brands.toml
                                  and write the market tracker dashboard
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .actions import ActionLog
from .agents.base import TEAMS, Context
from .config import Mode, Settings
from .data.sample import seed
from .data.warehouse import Warehouse
from .orchestrator import Orchestrator


def _log(wh: Warehouse, settings: Settings, executors: bool = True) -> ActionLog:
    ex = {}
    if executors and settings.mode is not Mode.RECOMMEND:
        from .executors import from_env

        ex = from_env(wh)
    return ActionLog(wh, settings.mode, ex)


def _market(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import market
    from .market import analyze, render

    if not args.no_collect:
        print(f"Snapshot saved to {market.save(market.collect())}")
    snaps = market.history()
    if not snaps:
        print("No market history yet; run without --no-collect first.")
        return 1
    out = render.write(analyze.build(snaps, market.load_config()), Path(args.out))
    print(f"Market tracker written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="amz-agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("--only", help="comma-separated: products,metrics,inventory,ads,"
                       "listing_assets,asin_traffic,costs,shopify,meta,ga4,pagespeed")
    p_ing.add_argument("--costs", help="CSV with sku, landed_cost, lead_time_days")
    p_imp = sub.add_parser("import")
    p_imp.add_argument("table")
    p_imp.add_argument("file")
    sub.add_parser("run").add_argument(
        "--no-email", action="store_true", help="don't email the brief even if SMTP is set"
    )
    p_list = sub.add_parser("actions")
    p_list.add_argument("--status")
    for name in ("approve", "reject", "done"):
        sub.add_parser(name).add_argument("ids", nargs="+", type=int)
    p_dash = sub.add_parser("dashboard")
    p_dash.add_argument("--out", default="reports/dashboard.html")
    p_dash.add_argument("--serve", action="store_true", help="serve it locally, always fresh")
    p_dash.add_argument("--host", default="127.0.0.1")
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.add_argument("--allow-actions", action="store_true",
                        help="show Approve / Reject / Mark done buttons (server mode)")
    p_mkt = sub.add_parser("market")
    p_mkt.add_argument("--no-collect", action="store_true",
                       help="rebuild the dashboard from saved history only")
    p_mkt.add_argument("--out", default="market/index.html")
    args = ap.parse_args(argv)
    if args.cmd == "market":
        return _market(args)

    settings = Settings.from_env()
    if args.cmd == "demo":
        settings = Settings(
            mode=Mode.RECOMMEND,
            warehouse_path="data/demo.db",
            anthropic_model=settings.anthropic_model,
            guardrails=settings.guardrails,
        )
    wh = Warehouse(settings.warehouse_path)

    if args.cmd in ("demo", "run"):
        if args.cmd == "demo":
            seed(wh)
        orch = Orchestrator(Context(wh, settings), _log(wh, settings))
        submitted = orch.run()
        path = orch.brief(submitted)
        teams = orch.team_briefs(submitted)
        text = path.read_text()
        print(text)
        print(f"Brief written to {path}")
        for team, p in teams.items():
            print(f"{TEAMS[team]} brief written to {p}")
        from .dashboard import write as write_dashboard

        print(f"Agent tracker written to {write_dashboard(wh)}")
        if args.cmd == "run" and not args.no_email:
            from .notify import send_brief, team_recipients_var

            day = path.stem.removeprefix("brief-")
            if send_brief(text, f"Daily brief: {day}"):
                print("Brief emailed.")
            for team, p in teams.items():
                if send_brief(p.read_text(), f"{TEAMS[team]} brief: {day}",
                              team_recipients_var(team)):
                    print(f"{TEAMS[team]} brief emailed.")
    elif args.cmd == "ingest":
        from . import ingest

        if args.costs:
            os.environ["COSTS_CSV"] = args.costs
        only = set(args.only.split(",")) if args.only else None
        results = ingest.run_all(wh, only)
        if len(results) == 1:
            print("No credentials configured; nothing to ingest. See .env.example.")
        for job, result in results.items():
            print(f"{job}: {result}")
    elif args.cmd == "import":
        from .ingest import import_csv

        print(f"{import_csv(wh, args.table, args.file)} rows into {args.table}")
    elif args.cmd == "dashboard":
        from . import dashboard

        if args.serve:
            dashboard.serve(wh, _log(wh, settings, executors=args.allow_actions),
                            args.host, args.port, args.allow_actions)
        else:
            print(f"Agent tracker written to {dashboard.write(wh, args.out)}")
    elif args.cmd == "actions":
        for a in _log(wh, settings, executors=False).list(args.status):
            print(json.dumps(a))
    else:
        log = _log(wh, settings)
        for i in args.ids:
            {"approve": log.approve, "reject": log.reject, "done": log.complete}[args.cmd](i)
            status = log.wh.query("SELECT status FROM actions WHERE id=?", (i,))[0]["status"]
            print(f"action {i}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
