"""Hard limits enforced in code. Agents (and LLMs) can propose anything;
nothing leaves the system without passing through these functions."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Guardrails


@dataclass
class Clamped:
    value: float
    clamped: bool
    note: str = ""


def clamp_bid(g: Guardrails, current: float, proposed: float) -> Clamped:
    lo = max(g.min_bid, current * (1 - g.max_bid_change_pct))
    hi = min(g.max_bid, current * (1 + g.max_bid_change_pct))
    value = round(min(max(proposed, lo), hi), 2)
    if value != round(proposed, 2):
        return Clamped(value, True, f"bid {proposed:.2f} clamped to {value:.2f} [{lo:.2f}, {hi:.2f}]")
    return Clamped(value, False)


def clamp_budget(g: Guardrails, proposed: float) -> Clamped:
    if proposed > g.max_daily_budget:
        return Clamped(g.max_daily_budget, True, f"budget capped at {g.max_daily_budget:.2f}")
    return Clamped(round(proposed, 2), False)


def clamp_budget_change(g: Guardrails, current: float, proposed: float) -> Clamped:
    """Daily budget moves at most max_budget_change_pct per change and never past the cap."""
    lo = current * (1 - g.max_budget_change_pct)
    hi = min(g.max_daily_budget, current * (1 + g.max_budget_change_pct))
    value = round(min(max(proposed, lo), hi), 2)
    if value != round(proposed, 2):
        return Clamped(value, True, f"budget {proposed:.2f} clamped to {value:.2f} [{lo:.2f}, {hi:.2f}]")
    return Clamped(value, False)


def price_floor(g: Guardrails, landed_cost: float, fees: float) -> float:
    """Lowest price that still clears the minimum margin on revenue."""
    return round((landed_cost + fees) / (1 - g.min_margin_pct), 2)


def clamp_price(
    g: Guardrails, current: float, proposed: float, landed_cost: float, fees: float
) -> Clamped:
    floor = price_floor(g, landed_cost, fees)
    lo = max(floor, current * (1 - g.max_price_change_pct))
    hi = current * (1 + g.max_price_change_pct)
    if lo > hi:  # current price is already below floor; allow moving up to floor
        return Clamped(floor, True, f"current price below floor; raise to {floor:.2f}")
    value = round(min(max(proposed, lo), hi), 2)
    if value != round(proposed, 2):
        return Clamped(value, True, f"price {proposed:.2f} clamped to {value:.2f} (floor {floor:.2f})")
    return Clamped(value, False)
