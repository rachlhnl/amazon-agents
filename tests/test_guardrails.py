from amz_agents.config import Guardrails
from amz_agents.guardrails import clamp_bid, clamp_budget, clamp_price, price_floor

G = Guardrails()


def test_bid_change_capped_per_day():
    assert clamp_bid(G, 1.00, 5.00).value == 1.20
    assert clamp_bid(G, 1.00, 0.10).value == 0.80


def test_bid_absolute_limits():
    assert clamp_bid(G, 0.22, 0.01).value == G.min_bid
    assert clamp_bid(G, 4.90, 9.00).value == G.max_bid


def test_bid_within_limits_untouched():
    c = clamp_bid(G, 1.00, 1.10)
    assert c.value == 1.10 and not c.clamped


def test_budget_cap():
    assert clamp_budget(G, 10_000).value == G.max_daily_budget


def test_price_never_below_floor():
    floor = price_floor(G, landed_cost=10, fees=5)
    assert floor == round(15 / 0.85, 2)
    assert clamp_price(G, 18.00, 1.00, 10, 5).value >= floor


def test_price_change_capped():
    assert clamp_price(G, 100.00, 200.00, 10, 5).value == 110.00


def test_price_below_floor_moves_up_to_floor():
    c = clamp_price(G, 10.00, 10.00, 10, 5)
    assert c.value == price_floor(G, 10, 5) and c.clamped
