"""UX agent: how well the store and the Amazon detail pages turn visits into sales.

  - Store funnel (sessions -> add to cart -> checkout -> purchase): the step
    furthest below benchmark, a step that suddenly got worse (a broken checkout),
    and mobile converting far below desktop.
  - Page speed and accessibility (Lighthouse via PageSpeed Insights).
  - Campaign landing pages: when an ad gets clicks but its page doesn't convert,
    says so on the marketing agent's budget proposal ("the ad works, the page
    doesn't") so nobody cuts a good ad for a bad page.
  - Amazon detail-page conversion (units per session) well below the rest of the
    catalog, with the likely cause (lost Buy Box vs. the page itself).

Everything here is work for a person (designers, developers); nothing executes.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from ..actions import Action
from .base import Agent, Context, Role

BENCHMARK = {"add_to_cart": 0.06, "checkout": 0.45, "purchase": 0.50}  # step conversion
WEAK_STEP = 0.8  # below this share of benchmark
STEP_DROP = -0.25  # vs the previous two weeks
MOBILE_GAP = 0.5  # mobile conversion under half of desktop's
MOBILE_TARGET = 0.7  # realistic mobile conversion as a share of desktop's
MIN_SESSIONS = 500  # per device / window before judging

SLOW_PERF = 50  # Lighthouse performance score (mobile)
SLOW_LCP_MS = 4000  # Google's "poor" threshold for largest contentful paint
POOR_CLS = 0.25
SPEED_LIFT = 0.10  # conversion lift from taking a page from poor to good speed
LOW_A11Y = 80

LANDING_MIN_SESSIONS = 200
LANDING_GAP = 0.5  # landing page converts under half the site rate
LANDING_TARGET = 0.8

AMAZON_MIN_SESSIONS = 300
AMAZON_GAP = 0.6  # unit session % under this share of the catalog median
AMAZON_TARGET = 0.8
BUY_BOX_OK = 0.8
DEFAULT_STORE_MARGIN = 0.35


def _norm(url: str | None) -> str:
    if not url:
        return ""
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")


class UXAgent(Agent):
    name = "ux"
    role = Role(
        mission="Turn more visits into sales: store funnel, mobile, page speed and "
                "accessibility, campaign landing pages, Amazon detail-page conversion, and a "
                "weekly mystery shop of our store against the other brands.",
        team="ux",
        proposes={"fix_funnel_step": "L", "improve_page_speed": "M", "fix_accessibility": "M",
                  "fix_landing_page": "M", "improve_listing_conversion": "M",
                  "improve_shopping_experience": "S"},
        needs=("web_funnel", "web_pages", "asin_traffic"),
    )

    def __init__(self, today: date | None = None):
        self.today = today or date.today()

    def run(self, ctx: Context) -> list[Action]:
        self.ctx = ctx
        self.aov, self.margin = self._store_economics()
        self.site_cr = self._site_conversion()
        return (self._funnel() + self._pages() + self._landing_pages()
                + self._amazon_conversion() + self._mystery_shop())

    def _mystery_shop(self) -> list[Action]:
        """What the mystery shopper missed on our store that other brands show."""
        out = []
        for r in self.ctx.wh.query("SELECT * FROM market_recs WHERE area='Website' ORDER BY rank"):
            out.append(Action(
                agent=self.name, kind="improve_shopping_experience",
                target=re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-")[:80],
                before=None,
                after={"title": r["title"], "when": r["rank"], "area": r["area"],
                       "steps": json.loads(r["actions"] or "[]"), "kpi": r["kpi"],
                       "kpi_target": r["kpi_target"], "url": r["url"], "source": "market"},
                reason=f"{r['see']} {r['why']}".strip(),
                notes=f"{r['kpi']} now {r['kpi_now']}" if r["kpi"] and r["kpi_now"] else "",
            ))
        return out

    # -------------------------------------------------------------- helpers
    def _window(self, start_ago: int, end_ago: int, device: str | None = None) -> dict:
        start = (self.today - timedelta(days=start_ago)).isoformat()
        end = (self.today - timedelta(days=end_ago)).isoformat()
        sql = ("SELECT COALESCE(SUM(sessions),0) AS sessions, "
               "COALESCE(SUM(add_to_cart),0) AS add_to_cart, COALESCE(SUM(checkout),0) AS checkout, "
               "COALESCE(SUM(purchases),0) AS purchases, COALESCE(SUM(revenue),0) AS revenue "
               "FROM web_funnel WHERE date BETWEEN ? AND ?")
        params: tuple = (start, end)
        if device:
            sql += " AND device=?"
            params += (device,)
        return self.ctx.wh.query(sql, params)[0]

    def _store_economics(self) -> tuple[float, float]:
        w = self._window(28, 1)
        aov = w["revenue"] / w["purchases"] if w["purchases"] else 0.0
        rows = self.ctx.wh.query(
            "SELECT c.price, c.fees, c.units_30d, p.landed_cost FROM channel_listings c "
            "JOIN products p ON p.sku = c.sku WHERE c.channel='shopify' AND c.listed = 1 "
            "AND p.landed_cost IS NOT NULL AND c.fees IS NOT NULL AND c.price > 0"
        )
        units = sum(r["units_30d"] or 0 for r in rows)
        if not units:
            return aov, DEFAULT_STORE_MARGIN
        margin = sum((r["price"] - r["landed_cost"] - r["fees"]) / r["price"] * (r["units_30d"] or 0)
                     for r in rows) / units
        return aov, margin

    def _site_conversion(self) -> float | None:
        w = self._window(28, 1)
        return w["purchases"] / w["sessions"] if w["sessions"] else None

    def _value(self, extra_orders: float) -> float | None:
        v = extra_orders * self.aov * self.margin
        return round(v, 2) if v > 0 else None

    def _action(self, kind: str, target: str, title: str, reason: str, recommendation: str,
                impact: float | None, sku: str | None = None, before: dict | None = None,
                related: list[tuple[str, str]] | None = None, confidence: str = "low") -> Action:
        return Action(
            agent=self.name, kind=kind, target=target, sku=sku, before=before,
            after={"title": title, "recommendation": recommendation, "impact_usd": impact,
                   "confidence": confidence, "related": [list(r) for r in related or []]},
            reason=reason,
        )

    @staticmethod
    def _rates(w: dict) -> dict[str, float | None]:
        return {
            "add_to_cart": w["add_to_cart"] / w["sessions"] if w["sessions"] else None,
            "checkout": w["checkout"] / w["add_to_cart"] if w["add_to_cart"] else None,
            "purchase": w["purchases"] / w["checkout"] if w["checkout"] else None,
        }

    # --------------------------------------------------------------- funnel
    def _funnel(self) -> list[Action]:
        out: list[Action] = []
        now, prev = self._window(14, 1), self._window(28, 15)
        if now["sessions"] < MIN_SESSIONS:
            return out
        rates, prev_rates = self._rates(now), self._rates(prev)
        monthly_orders = now["purchases"] / 14 * 30

        # A step that suddenly got worse is the most urgent thing in the funnel.
        dropped = set()
        for step, r in rates.items():
            p = prev_rates.get(step)
            if r is None or not p or prev["sessions"] < MIN_SESSIONS:
                continue
            change = r / p - 1
            if change <= STEP_DROP:
                dropped.add(step)
                where = self._worst_device(step)
                out.append(self._action(
                    "fix_funnel_step", f"funnel/{step}-drop",
                    title=f"Store {step.replace('_', ' ')} rate fell {abs(change):.0%}",
                    reason=(f"{r:.1%} in the last two weeks vs {p:.1%} the two weeks before"
                            + (f", almost all of it on {where}" if where else "") + ". "
                            "Sudden drops usually mean something broke: a theme or app update, "
                            "a payment method, shipping rates, a discount code."),
                    recommendation="Walk the step on a phone and a desktop today; check "
                                   "recent theme/app changes and the payment/shipping settings.",
                    impact=self._value(monthly_orders * (p / r - 1)), confidence="medium",
                ))

        weakest = min(((s, r / BENCHMARK[s]) for s, r in rates.items()
                       if r is not None and s not in dropped), key=lambda x: x[1], default=None)
        if weakest and weakest[1] < WEAK_STEP:
            step, ratio = weakest
            out.append(self._action(
                "fix_funnel_step", f"funnel/{step}",
                title=f"Store {step.replace('_', ' ')} rate is the weakest step",
                reason=(f"{rates[step]:.1%} vs a typical {BENCHMARK[step]:.0%}: "
                        f"{ratio:.0%} of benchmark, the biggest gap in the funnel."),
                recommendation={
                    "add_to_cart": "Product pages: price, shipping and returns visible above "
                                   "the fold, better images, reviews on the page.",
                    "checkout": "Cart: show total cost (shipping, tax) before checkout, add "
                                "express pay (Shop Pay, Apple Pay), remove distractions.",
                    "purchase": "Checkout: fewer fields, guest checkout, more payment "
                                "options, no surprise costs at the last step.",
                }[step],
                impact=self._value(monthly_orders * (min(1 / ratio, 2) - 1)),
            ))

        mob, desk = self._window(14, 1, "mobile"), self._window(14, 1, "desktop")
        if mob["sessions"] >= MIN_SESSIONS and desk["sessions"] >= MIN_SESSIONS and \
                desk["purchases"]:
            m_cr, d_cr = mob["purchases"] / mob["sessions"], desk["purchases"] / desk["sessions"]
            if m_cr < d_cr * MOBILE_GAP:
                extra = mob["sessions"] / 14 * 30 * (d_cr * MOBILE_TARGET - m_cr)
                m_rates, d_rates = self._rates(mob), self._rates(desk)
                worst = min((s for s in m_rates if m_rates[s] and d_rates[s]),
                            key=lambda s: m_rates[s] / d_rates[s], default=None)
                out.append(self._action(
                    "fix_funnel_step", "funnel/mobile",
                    title="Mobile converts far below desktop",
                    reason=(f"mobile {m_cr:.2%} vs desktop {d_cr:.2%} of sessions buy; "
                            + (f"the gap is widest at {worst.replace('_', ' ')} "
                               f"({m_rates[worst]:.1%} vs {d_rates[worst]:.1%})."
                               if worst else "")),
                    recommendation="Test the store on a mid-range phone: tap targets, sticky "
                                   "add-to-cart, image load, pop-ups covering the page, "
                                   "express pay at the top of checkout.",
                    impact=self._value(extra),
                ))
        return out

    def _worst_device(self, step: str) -> str | None:
        """The device whose rate at this step fell the most, if it explains the drop."""
        changes = {}
        for d in ("mobile", "desktop", "tablet"):
            now, prev = self._rates(self._window(14, 1, d)), self._rates(self._window(28, 15, d))
            if now[step] is not None and prev[step]:
                changes[d] = now[step] / prev[step] - 1
        if not changes:
            return None
        worst = min(changes, key=changes.get)
        others = [c for d, c in changes.items() if d != worst]
        return worst if changes[worst] <= STEP_DROP and all(c > STEP_DROP for c in others) \
            else None

    # ----------------------------------------------------------- page speed
    def _pages(self) -> list[Action]:
        out: list[Action] = []
        for p in self.ctx.wh.query("SELECT * FROM web_pages WHERE perf_mobile IS NOT NULL"):
            slow = (p["perf_mobile"] or 100) < SLOW_PERF or (p["lcp_ms_mobile"] or 0) > SLOW_LCP_MS
            if slow:
                orders = p["purchases_30d"] or 0  # through this page, last 30 days
                facts = [f"mobile performance {p['perf_mobile']}/100"]
                if p["lcp_ms_mobile"]:
                    facts.append(f"main content loads in {p['lcp_ms_mobile'] / 1000:.1f}s")
                if (p["cls_mobile"] or 0) > POOR_CLS:
                    facts.append(f"layout shifts {p['cls_mobile']:.2f}")
                out.append(self._action(
                    "improve_page_speed", p["url"], sku=p["sku"],
                    title=f"Speed up {p['page_type'] or 'page'} {urlsplit(p['url']).path or '/'}",
                    reason=", ".join(facts) + ".",
                    recommendation="Compress and lazy-load images, cut unused apps/scripts, "
                                   "set image dimensions to stop layout shift.",
                    impact=self._value(orders * SPEED_LIFT),
                    before={"perf_mobile": p["perf_mobile"], "lcp_ms": p["lcp_ms_mobile"]},
                ))
            if p["accessibility"] is not None and p["accessibility"] < LOW_A11Y:
                out.append(self._action(
                    "fix_accessibility", f"a11y/{p['url']}", sku=p["sku"],
                    title=f"Accessibility issues on {urlsplit(p['url']).path or '/'}",
                    reason=f"Lighthouse accessibility {p['accessibility']}/100.",
                    recommendation="Fix contrast, image alt text, form labels and button "
                                   "names from the Lighthouse report. Also a legal risk.",
                    impact=None, confidence="medium",
                ))
        return out

    # -------------------------------------------------------- landing pages
    def _landing_pages(self) -> list[Action]:
        if not self.site_cr:
            return []
        pages = {_norm(p["url"]): p for p in self.ctx.wh.query("SELECT * FROM web_pages")}
        proposals = self.ctx.shared.get("proposals", [])
        landing: dict[str, set[str]] = {}
        for c in self.ctx.wh.query(
            "SELECT DISTINCT campaign_id, landing_url FROM creatives WHERE landing_url IS NOT NULL"
        ):
            landing.setdefault(_norm(c["landing_url"]), set()).add(c["campaign_id"])

        out: list[Action] = []
        for url, campaign_ids in sorted(landing.items()):
            p = pages.get(url)
            if not p or (p["sessions_30d"] or 0) < LANDING_MIN_SESSIONS:
                continue
            cr = (p["purchases_30d"] or 0) / p["sessions_30d"]
            if cr >= self.site_cr * LANDING_GAP:
                continue
            related = [(a.kind, a.target) for a in proposals
                       if a.target in campaign_ids
                       and a.kind in ("update_campaign_budget", "pause_campaign")]
            # Tell the marketing agent's proposals: the page may be the problem.
            for a in proposals:
                if (a.kind, a.target) in related:
                    note = (f"UX: its landing page converts {cr:.2%} vs {self.site_cr:.2%} "
                            "site-wide; fix the page before judging the ad")
                    a.notes = "; ".join(x for x in (a.notes, note) if x)
            extra = p["sessions_30d"] * (self.site_cr * LANDING_TARGET - cr)
            out.append(self._action(
                "fix_landing_page", url, sku=p["sku"],
                title=f"Landing page {urlsplit(url).path or '/'} loses ad traffic",
                reason=(f"{p['sessions_30d']:,} visits from ads in 30 days convert at {cr:.2%} "
                        f"vs {self.site_cr:.2%} for the store: the ads get the click, the page "
                        "doesn't close."),
                recommendation="Match the page to the ad's promise (same product, image and "
                               "offer up top), one clear add-to-cart, fast on mobile.",
                impact=self._value(extra), related=related, confidence="medium",
            ))
        return out

    # ------------------------------------------------------ Amazon conversion
    def _amazon_conversion(self) -> list[Action]:
        rows = [r for r in self.ctx.wh.query("SELECT * FROM asin_traffic")
                if (r["sessions_30d"] or 0) >= AMAZON_MIN_SESSIONS]
        if len(rows) < 2:
            return []
        crs = sorted(r["units_30d"] / r["sessions_30d"] for r in rows)
        median = crs[len(crs) // 2] if len(crs) % 2 else (crs[len(crs) // 2 - 1]
                                                          + crs[len(crs) // 2]) / 2
        products = {p["sku"]: p for p in self.ctx.wh.query("SELECT * FROM products")}
        proposals = self.ctx.shared.get("proposals", [])
        out: list[Action] = []
        for r in rows:
            cr = r["units_30d"] / r["sessions_30d"]
            if cr >= median * AMAZON_GAP:
                continue
            p = products.get(r["sku"], {})
            profit = (p["price"] - p["landed_cost"] - p["fees"]
                      if p and None not in (p["price"], p["landed_cost"], p["fees"]) else 0)
            bb = r["buy_box_pct"]
            lost_bb = bb is not None and bb < BUY_BOX_OK
            related = [(a.kind, a.target) for a in proposals
                       if r["sku"] in (a.sku, a.target) and a.kind in
                       ({"update_price"} if lost_bb else
                        {"update_listing_copy", "improve_listing_assets", "flag_defect",
                         "fix_listing_title"})]
            out.append(self._action(
                "improve_listing_conversion", r["sku"], sku=r["sku"],
                title=f"{r['sku']} detail page converts poorly",
                reason=(f"{cr:.1%} of {r['sessions_30d']:,} sessions bought vs {median:.1%} "
                        "median across the catalog. "
                        + (f"You held the Buy Box only {bb:.0%} of the time: that's the likely "
                           "cause, not the page." if lost_bb else
                           "Buy Box is fine, so the page itself (images, price perception, "
                           "reviews) is losing shoppers.")),
                recommendation=("Fix Buy Box share first (see pricing)." if lost_bb else
                                "Compare the page with the best-converting SKU: images, "
                                "bullets, A+, review rating; test a new main image."),
                impact=round(r["sessions_30d"] * (median * AMAZON_TARGET - cr) * profit, 2)
                or None,
                related=related, confidence="medium" if lost_bb else "low",
            ))
        return out
