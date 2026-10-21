"""Creative agent: the ads, images and words customers see.

  - Ad creative (outside Amazon): spots fatigue (clicks per view falling while the
    same people see the ad more often), pauses ads that spend without selling
    while their siblings sell, and asks for variants of ads that clearly win.
  - Amazon detail pages: images, video, A+ content, bullets, and titles that break
    brand or Amazon rules.
  - Review: every draft the other agents wrote this run (marketing emails,
    wholesale outreach, listing copy) is checked against brand.toml before it
    reaches a person, and comes back marked "ok" or "needs changes" with what to fix.

Pausing an ad goes through the Meta API when approved; everything else is for a
person (designers, copywriters).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .. import llm
from ..actions import Action
from .base import Agent, Context, Role
from .marketing import BRAND_REFERRAL_BONUS, unit_margin_pct

MIN_IMPRESSIONS = 2000  # per window, before judging an ad's click rate
FATIGUE_CTR_DROP = -0.30  # click rate last 7 days vs days 8-28
FATIGUE_FREQUENCY = 2.5  # average views per person in the last 7 days
PAUSE_MIN_SPEND = 100.0  # 28-day spend with no purchases in the last 7 days
PAUSE_CTR_RATIO = 0.5  # and a click rate under half its campaign's
WINNER_CTR_RATIO = 1.5
WINNER_MIN_PURCHASES = 3

MIN_IMAGES = 7  # Amazon allows 9; 7+ is a common floor for a complete gallery
MIN_BULLETS = 5
ASSET_LIFT = 0.05  # conversion lift from a complete detail page (rough)
DRAFT_KINDS = {"email_campaign", "send_outreach", "update_listing_copy"}

DEFAULT_LIMITS = {"email_subject_chars": 60, "email_body_words": 150, "outreach_words": 150,
                  "listing_title_chars": 200, "max_exclamations": 1}


SIZE_CODE = re.compile(r"X*[SML]|X+L|X+S")  # XXXL, XXS: sizes, not shouting

def load_brand(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path or os.getenv("BRAND_PATH", "brand.toml"))
    brand: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as f:
            brand = tomllib.load(f)
    brand["limits"] = {**DEFAULT_LIMITS, **brand.get("limits", {})}
    return brand


def review_copy(text: str, kind: str, brand: dict[str, Any]) -> list[str]:
    """Rule-based checks against brand.toml. Returns what to fix (empty = ok)."""
    issues = []
    lim = brand["limits"]
    for w in brand.get("banned_words", []):
        if re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE):
            issues.append(f"uses banned word '{w}'")
    for pat in brand.get("banned_claims", []):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            issues.append(f"unsubstantiated claim '{m.group(0)}'")
    for c in brand.get("competitors", []):
        if re.search(rf"\b{re.escape(c)}\b", text, re.IGNORECASE):
            issues.append(f"names competitor '{c}'")
    if text.count("!") > lim["max_exclamations"]:
        issues.append(f"{text.count('!')} exclamation marks (max {lim['max_exclamations']})")
    shouting = [w for w in re.findall(r"\b[A-Z]{4,}\b", text)
                if not re.search(r"\d", w) and not SIZE_CODE.fullmatch(w)]
    if shouting:
        issues.append(f"all-caps words: {', '.join(sorted(set(shouting))[:3])}")

    if kind == "email_campaign":
        subject = next((ln.split(":", 1)[1].strip() for ln in text.splitlines()
                        if ln.lower().startswith("subject:")), "")
        if len(subject) > lim["email_subject_chars"]:
            issues.append(f"subject is {len(subject)} characters (max "
                          f"{lim['email_subject_chars']})")
        words = len(re.findall(r"\w+", text)) - len(re.findall(r"\w+", subject))
        if words > lim["email_body_words"]:
            issues.append(f"body is {words} words (max {lim['email_body_words']})")
    elif kind == "send_outreach":
        words = len(re.findall(r"\w+", text))
        if words > lim["outreach_words"]:
            issues.append(f"{words} words (max {lim['outreach_words']})")
    elif kind in ("update_listing_copy", "listing_title"):
        title = text.splitlines()[0] if text else ""
        if len(title) > lim["listing_title_chars"]:
            issues.append(f"title is {len(title)} characters (max {lim['listing_title_chars']})")
    return issues


class CreativeAgent(Agent):
    name = "creative"
    role = Role(
        mission="Keep what customers see working and on-brand: ad creative fatigue and "
                "winners, Amazon images/video/A+, and a brand review of every draft.",
        team="creative",
        proposes={"refresh_creative": "M", "pause_creative": "XS",
                  "new_creative_variants": "M", "improve_listing_assets": "M",
                  "fix_listing_title": "S"},
        needs=("creatives", "listing_assets"),
    )

    def __init__(self, brand: dict[str, Any] | None = None):
        self.brand = brand

    def run(self, ctx: Context) -> list[Action]:
        self.brand = self.brand or load_brand()
        ctx.shared["creative_reviews"] = self._review_drafts(ctx)
        return self._creatives(ctx) + self._listings(ctx)

    # --------------------------------------------------------- draft review
    def _review_drafts(self, ctx: Context) -> int:
        """Mark each draft in this run's proposals ok / needs changes. Returns count."""
        n = 0
        for a in ctx.shared.get("proposals", []):
            if a.kind not in DRAFT_KINDS or not isinstance(a.after, dict):
                continue
            draft = a.after.get("draft")
            if not draft:
                continue
            issues = review_copy(draft, a.kind, self.brand) + self._llm_review(ctx, draft, a.kind)
            a.after["creative_review"] = {"status": "needs changes" if issues else "ok",
                                          "issues": issues}
            note = ("creative review: needs changes: " + "; ".join(issues) if issues
                    else "creative review: ok")
            a.notes = "; ".join(x for x in (a.notes, note) if x)
            n += 1
        return n

    def _llm_review(self, ctx: Context, draft: str, kind: str) -> list[str]:
        voice = self.brand.get("voice")
        if not voice:
            return []
        out = llm.complete(
            "You review marketing copy for a consumer brand against its voice. Return only "
            'JSON: {"issues": ["..."]}, listing concrete problems (empty list if none). '
            "Don't nitpick; flag what a brand lead would send back.",
            f"Brand voice:\n{voice}\n\nCopy type: {kind}\n\nCopy:\n{draft}",
            ctx.settings.anthropic_model, max_tokens=400,
        )
        if not out:
            return []
        try:
            text = out.strip().removeprefix("```json").strip("`\n ")
            return [f"voice: {x}" for x in json.loads(text).get("issues", [])][:5]
        except (json.JSONDecodeError, AttributeError):
            return []

    # ------------------------------------------------------------ creatives
    def _creatives(self, ctx: Context) -> list[Action]:
        low = ctx.shared.get("low_stock", set())
        campaigns = {c["campaign_id"]: c for c in ctx.wh.query("SELECT * FROM external_campaigns")}
        ads = ctx.wh.query("SELECT * FROM creatives WHERE status='enabled'")
        by_campaign: dict[str, list[dict]] = {}
        for ad in ads:
            by_campaign.setdefault(ad["campaign_id"], []).append(ad)

        out: list[Action] = []
        for ad in ads:
            c = campaigns.get(ad["campaign_id"])
            ctr7 = _ctr(ad["clicks_7d"], ad["impressions_7d"])
            ctr_prior = _ctr(ad["clicks_prior"], ad["impressions_prior"])
            siblings = by_campaign.get(ad["campaign_id"], [])
            camp_ctr = _ctr(sum(s["clicks_7d"] or 0 for s in siblings),
                            sum(s["impressions_7d"] or 0 for s in siblings))
            value_per_click = self._value_per_click(ctx, c)
            before = {"name": ad["name"], "platform": ad["platform"],
                      "campaign": c["name"] if c else ad["campaign_id"],
                      "ctr_7d": round(ctr7, 4) if ctr7 is not None else None}

            if (ad["impressions_7d"] or 0) >= MIN_IMPRESSIONS and \
                    (ad["impressions_prior"] or 0) >= MIN_IMPRESSIONS and ctr7 and ctr_prior:
                drop = ctr7 / ctr_prior - 1
                if drop <= FATIGUE_CTR_DROP and (ad["frequency_7d"] or 0) >= FATIGUE_FREQUENCY:
                    lost_clicks = ad["impressions_7d"] / 7 * 30 * (ctr_prior - ctr7)
                    out.append(self._action(
                        "refresh_creative", ad, before,
                        title=f"Refresh fatigued ad '{ad['name']}'",
                        reason=(f"click rate {ctr7:.2%} vs {ctr_prior:.2%} before ({drop:.0%}) "
                                f"while each person saw it {ad['frequency_7d']:.1f}× this week"),
                        recommendation="New hook and first frame on the same offer; keep the "
                                       "landing page. Rotate it in alongside the current ad.",
                        impact=lost_clicks * value_per_click if value_per_click else None,
                    ))
                    continue

            spend28 = (ad["spend_7d"] or 0) + (ad["spend_prior"] or 0)
            if (len(siblings) > 1 and spend28 >= PAUSE_MIN_SPEND and not ad["purchases_7d"]
                    and ctr7 is not None and camp_ctr and ctr7 < camp_ctr * PAUSE_CTR_RATIO):
                ppd = (value_per_click * (c["clicks_30d"] or 0) / c["spend_30d"]
                       if c and c["spend_30d"] and value_per_click else None)
                out.append(self._action(
                    "pause_creative", ad, before,
                    title=f"Pause ad '{ad['name']}'",
                    reason=(f"${spend28:.0f} over 4 weeks, no purchases this week, click rate "
                            f"{ctr7:.2%} vs {camp_ctr:.2%} for its campaign"),
                    recommendation="Pause it; the platform shifts its budget to the ads that "
                                   "sell.",
                    impact=(ad["spend_7d"] or 0) / 7 * 30 * ppd if ppd else None,
                    after={"status": "paused"},
                ))
            elif (len(siblings) > 1 and ad["sku"] not in low and camp_ctr and ctr7
                  and ctr7 >= camp_ctr * WINNER_CTR_RATIO
                  and (ad["purchases_7d"] or 0) >= WINNER_MIN_PURCHASES):
                out.append(self._action(
                    "new_creative_variants", ad, before,
                    title=f"Make variants of winning ad '{ad['name']}'",
                    reason=(f"click rate {ctr7:.2%} vs {camp_ctr:.2%} for its campaign, "
                            f"{ad['purchases_7d']} purchases this week"),
                    recommendation="Brief 2–3 variants on the same hook (new visuals, same "
                                   "message) before this one fatigues.",
                    impact=None,
                ))
        return out

    def _value_per_click(self, ctx: Context, c: dict | None) -> float | None:
        """Contribution per click for the campaign (sales x margin, + referral bonus)."""
        if not c or not c["clicks_30d"]:
            return None
        margin = unit_margin_pct(ctx, c["sku"], c["destination"])
        if margin is None:
            return None
        bonus = BRAND_REFERRAL_BONUS if c["destination"] == "amazon" else 0.0
        return (c["sales_30d"] or 0) * (margin + bonus) / c["clicks_30d"]

    def _action(self, kind: str, ad: dict, before: dict, title: str, reason: str,
                recommendation: str, impact: float | None,
                after: dict | None = None) -> Action:
        return Action(
            agent=self.name, kind=kind, target=ad["ad_id"], sku=ad["sku"], before=before,
            after={**(after or {}), "title": title, "recommendation": recommendation,
                   "impact_usd": round(impact, 2) if impact else None, "confidence": "low"},
            reason=reason,
        )

    # -------------------------------------------------------- listing assets
    def _listings(self, ctx: Context) -> list[Action]:
        assets = {r["sku"]: r for r in ctx.wh.query("SELECT * FROM listing_assets")}
        traffic = {r["sku"]: r for r in ctx.wh.query("SELECT * FROM asin_traffic")}
        out: list[Action] = []
        for p in ctx.wh.query("SELECT * FROM products ORDER BY sku"):
            a = assets.get(p["sku"])
            if a:
                gaps = []
                if (a["image_count"] or 0) < MIN_IMAGES:
                    gaps.append(f"{a['image_count'] or 0} images (aim for {MIN_IMAGES}+)")
                if a["has_video"] == 0:
                    gaps.append("no video")
                if a["has_aplus"] == 0:
                    gaps.append("no A+ content")
                if a["bullet_count"] is not None and a["bullet_count"] < MIN_BULLETS:
                    gaps.append(f"{a['bullet_count']} bullets (use {MIN_BULLETS})")
                if gaps:
                    t = traffic.get(p["sku"], {})
                    units = t.get("units_30d") or (p["daily_velocity"] or 0) * 30
                    profit = (p["price"] - p["landed_cost"] - p["fees"]
                              if None not in (p["price"], p["landed_cost"], p["fees"]) else 0)
                    impact = units * ASSET_LIFT * profit
                    out.append(Action(
                        agent=self.name, kind="improve_listing_assets", target=p["sku"],
                        sku=p["sku"], before={k: a[k] for k in a if k != "sku"},
                        after={"title": f"Complete the {p['sku']} detail page", "gaps": gaps,
                               "recommendation": "Brief design: lifestyle and size-comparison "
                                                 "images, a 30-second use video, A+ modules "
                                                 "answering the top review complaints.",
                               "impact_usd": round(impact, 2) or None, "confidence": "low"},
                        reason="detail page is missing " + ", ".join(gaps),
                    ))
            issues = review_copy(p["title"] or "", "listing_title", self.brand)
            if issues:
                out.append(Action(
                    agent=self.name, kind="fix_listing_title", target=p["sku"], sku=p["sku"],
                    before={"title": p["title"]},
                    after={"title": f"Fix the {p['sku']} title", "issues": issues,
                           "recommendation": "Rewrite without the flagged words; Amazon can "
                                             "suppress listings for promotional claims.",
                           "impact_usd": None, "confidence": "medium"},
                    reason="title " + "; ".join(issues),
                ))
        return out


def _ctr(clicks: int | None, impressions: int | None) -> float | None:
    return clicks / impressions if impressions else None
