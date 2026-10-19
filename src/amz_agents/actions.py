"""Actions: the only way agents change anything.

Every action is logged with before/after values and a reason, so lift can be
attributed and changes rolled back. What happens next depends on Mode:
  recommend -> status 'proposed' (never executed)
  approve   -> status 'pending_approval' until a human approves
  auto      -> executed immediately (guardrails already applied)
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


class Executor(Protocol):
    def __call__(self, action: dict[str, Any]) -> None: ...


# Customer-facing or real-world actions that always need a human, even in auto mode.
HUMAN_ONLY = {"update_listing_copy", "reorder", "flag_defect"}


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
            "INSERT INTO actions (agent, kind, target, before, after, reason, status, notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                action.agent,
                action.kind,
                action.target,
                json.dumps(action.before),
                json.dumps(action.after),
                action.reason,
                status,
                action.notes,
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
            self.wh.execute("UPDATE actions SET status='executed' WHERE id=?", (action_id,))
        except Exception as e:  # noqa: BLE001 - log the failure, don't crash the run
            self.wh.execute(
                "UPDATE actions SET status='failed', notes=? WHERE id=?", (repr(e), action_id)
            )
