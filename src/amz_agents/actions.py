"""Actions: the only way agents change anything.

Every action is logged with before/after values and a reason, so lift can be
attributed and changes rolled back. What happens next depends on Mode:
  recommend -> status 'proposed' (never executed)
  approve   -> status 'pending_approval' until a human approves
  auto      -> executed immediately (guardrails already applied)

Human-only work, once approved, is 'acknowledged' (a person is on it) until
someone marks it 'done'.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Mode
from .data.warehouse import Warehouse


@dataclass
class Action:
    agent: str
    kind: str
    target: str
    before: Any
    after: Any
    reason: str
    notes: str = ""
    # Filled in by the chief of staff before submission.
    sku: str | None = None
    impact_usd: float | None = None
    confidence: str = ""
    goal: str = ""
    team: str = ""  # team that owns the work (from the proposing agent's role)
    effort: str = ""  # XS..XL level of effort for a person
    roi: float | None = None  # impact / cost of that effort
    priority: str = ""  # now | high | medium | low | judgment


class Executor(Protocol):
    def __call__(self, action: dict[str, Any]) -> None: ...


# Customer-facing or real-world actions that always need a human, even in auto mode.
HUMAN_ONLY = {
    "update_listing_copy", "reorder", "flag_defect", "escalation",
    "list_on_channel", "send_outreach", "email_campaign", "opportunity",
    # creative and UX work is done by designers, copywriters and developers
    "refresh_creative", "new_creative_variants", "improve_listing_assets", "fix_listing_title",
    "fix_funnel_step", "improve_page_speed", "fix_accessibility", "fix_landing_page",
    "improve_listing_conversion",
    # product work: patterns, size charts, factory orders, launches
    "fix_fit", "restock_style", "improve_product", "plan_launch", "improve_shopping_experience",
}

# Statuses that mean an action is still waiting, or was carried out recently
# enough that proposing it again would duplicate it (e.g. a second identical
# negative keyword while the 30-day search-term report still shows the term).
OPEN_OR_DONE = ("pending_approval", "approved", "executed", "acknowledged", "done")
REPEAT_LOOKBACK_DAYS = 30


class ActionLog:
    def __init__(self, wh: Warehouse, mode: Mode, executors: dict[str, Executor] | None = None):
        self.wh = wh
        self.mode = mode
        self.executors: dict[str, Executor] = executors or {}

    def submit(self, action: Action) -> int:
        status = {
            Mode.RECOMMEND: "proposed",
            Mode.APPROVE: "pending_approval",
            Mode.AUTO: "approved",
        }[self.mode]
        if self.mode is Mode.AUTO and action.kind in HUMAN_ONLY:
            status = "pending_approval"
        action_id = self.wh.execute(
            "INSERT INTO actions (agent, kind, target, before, after, reason, status, notes, "
            "sku, impact_usd, confidence, goal, team, effort, roi, priority) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                action.agent,
                action.kind,
                action.target,
                json.dumps(action.before),
                json.dumps(action.after),
                action.reason,
                status,
                action.notes,
                action.sku,
                action.impact_usd,
                action.confidence,
                action.goal,
                action.team,
                action.effort,
                action.roi,
                action.priority,
            ),
        )
        if status == "approved":
            self._execute(action_id)
        return action_id

    def approve(self, action_id: int) -> None:
        self._set_status(action_id, "approved", expect="pending_approval")
        self._execute(action_id)

    def reject(self, action_id: int) -> None:
        self._set_status(action_id, "rejected", expect="pending_approval")

    def complete(self, action_id: int) -> None:
        """A person finished human-only work (placed the PO, published the copy...)."""
        self._set_status(action_id, "done", expect="acknowledged")
        self.wh.execute("UPDATE actions SET done_at=date('now') WHERE id=?", (action_id,))

    def already_open_or_done(self, action: Action) -> bool:
        """True if the same change is awaiting approval or was carried out recently.

        Anything for the same kind and target still awaiting approval counts, even with
        different numbers (a reorder whose quantity drifted by a unit). Done work counts
        only if identical, except opportunities: once acknowledged, an opportunity stays
        quiet for the lookback period even as its numbers move.
        """
        rows = self.wh.query(
            "SELECT status, after FROM actions WHERE kind=? AND target=? AND status IN "
            f"({','.join('?' * len(OPEN_OR_DONE))}) AND created >= date('now', ?)",
            (action.kind, action.target, *OPEN_OR_DONE, f"-{REPEAT_LOOKBACK_DAYS} days"),
        )
        after = json.dumps(action.after, sort_keys=True)
        return any(
            r["status"] == "pending_approval" or action.kind == "opportunity"
            or json.dumps(json.loads(r["after"]), sort_keys=True) == after
            for r in rows
        )

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.wh.query("SELECT * FROM actions WHERE status=? ORDER BY id", (status,))
        return self.wh.query("SELECT * FROM actions ORDER BY id")

    # --- internals ---
    def _set_status(self, action_id: int, status: str, expect: str | None = None) -> None:
        row = self.wh.query("SELECT status FROM actions WHERE id=?", (action_id,))
        if not row:
            raise KeyError(f"action {action_id} not found")
        if expect and row[0]["status"] != expect:
            raise ValueError(f"action {action_id} is {row[0]['status']}, expected {expect}")
        self.wh.execute("UPDATE actions SET status=? WHERE id=?", (status, action_id))

    def _execute(self, action_id: int) -> None:
        row = self.wh.query("SELECT * FROM actions WHERE id=?", (action_id,))[0]
        row["before"], row["after"] = json.loads(row["before"]), json.loads(row["after"])
        executor: Callable | None = self.executors.get(row["kind"])
        if executor is None and row["kind"] in HUMAN_ONLY:
            # Approved for a human to carry out (place the PO, edit the listing...).
            self.wh.execute("UPDATE actions SET status='acknowledged' WHERE id=?", (action_id,))
            return
        if executor is None:
            self.wh.execute(
                "UPDATE actions SET status='failed', notes=? WHERE id=?",
                (f"no executor registered for {row['kind']}", action_id),
            )
            return
        try:
            executor(row)
            self.wh.execute(
                "UPDATE actions SET status='executed', executed_at=date('now') WHERE id=?",
                (action_id,),
            )
        except Exception as e:  # noqa: BLE001 - log the failure, don't crash the run
            self.wh.execute(
                "UPDATE actions SET status='failed', notes=? WHERE id=?", (repr(e), action_id)
            )
