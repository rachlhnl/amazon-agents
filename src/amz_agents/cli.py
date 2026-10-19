"""Command line entry point.

  amz-agents demo                 seed sample data and run all agents
  amz-agents run [--no-email]     run all agents, write the brief, email it if SMTP is set
  amz-agents actions [--status S] list logged actions
  amz-agents approve ID [ID ...]  approve and execute pending actions
  amz-agents reject ID [ID ...]   reject pending actions
"""

from __future__ import annotations

import argparse
import json
import sys

from .actions import ActionLog
from .agents.base import Context
from .config import Mode, Settings
from .data.sample import seed
from .data.warehouse import Warehouse
from .orchestrator import Orchestrator


def _log(wh: Warehouse, settings: Settings) -> ActionLog:
    executors = {}
    if settings.mode is not Mode.RECOMMEND:
        from .data.clients import AdsClient, SPAPIClient
        from .executors import build_executors

        executors = build_executors(wh, AdsClient.from_env(), SPAPIClient.from_env())
    return ActionLog(wh, settings.mode, executors)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="amz-agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sub.add_parser("run").add_argument(
        "--no-email", action="store_true", help="don't email the brief even if SMTP is set"
    )
    p_list = sub.add_parser("actions")
    p_list.add_argument("--status")
    for name in ("approve", "reject"):
        sub.add_parser(name).add_argument("ids", nargs="+", type=int)
    args = ap.parse_args(argv)

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
        text = path.read_text()
        print(text)
        print(f"Brief written to {path}")
        if args.cmd == "run" and not args.no_email:
            from .notify import send_brief

            if send_brief(text, f"Amazon brief: {path.stem.removeprefix('brief-')}"):
                print("Brief emailed.")
    elif args.cmd == "actions":
        for a in _log(wh, settings).list(args.status):
            print(json.dumps(a))
    else:
        log = _log(wh, settings)
        for i in args.ids:
            (log.approve if args.cmd == "approve" else log.reject)(i)
            status = log.wh.query("SELECT status FROM actions WHERE id=?", (i,))[0]["status"]
            print(f"action {i}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
