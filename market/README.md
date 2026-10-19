# Market tracker

A daily dashboard of public data on Dagacci, its closest Amazon rival (Cozyfit) and
other scrub brands, with trends, opportunities and a plain-language read under every
chart. Open `index.html` in a browser.

It follows the layout of the Seller Central KPI dashboard: a headline number and KPI
tiles, "Where to act first" cards (what we see, why it matters, what to do, and the KPI
to track with a target), the full ranked action plan, then numbered sections, each
ending with "What this tells us · what to do".

| Section | Shows | Source |
|---|---|---|
| 1 · Customer sentiment | store reviews by quarter and topic, complaints by product, Amazon "Customers say" | Dagacci store reviews, Amazon product pages |
| 2 · Catalog, price & stock | catalog, ratings, prices, stock, tracked Amazon listings | dagacci.com, Amazon product pages |
| 3 · Dagacci vs Cozyfit on Amazon | price, rating, reviews and best-seller rank per listing | Amazon product pages (Cozyfit sells only on Amazon) |
| 4 · The scrub market | outfit price, markdowns, launches, stock and colors by brand | each brand's own store |
| 5 · Trends & changes | daily and weekly moves, what changed since the last run | all of the above, over time |

## How it runs

`.github/workflows/market.yml` runs `amz-agents market` every morning: it saves the
day's snapshot to `history/YYYY-MM-DD.json.gz`, adds new store reviews to `reviews/`, rebuilds `index.html` from all
snapshots, and commits both. Trend lines start on the second day and week-over-week
reads on the eighth. Run it by hand with:

```sh
amz-agents market                 # collect today and rebuild
amz-agents market --no-collect    # rebuild from saved history only
```

Brands, stores and the Amazon listings followed are in `brands.toml`. Add an ASIN
there to follow another listing; one per listing family is enough.

## Data notes

- **Stores:** Shopify's public `/products.json` catalog (prices, compare-at prices,
  color/size options and whether each can be bought) and, for Dagacci, the Judge.me
  rating on each product page.
- **Amazon:** read from public product pages, throttled to one every few seconds.
  This is against Amazon's Conditions of Use and Amazon blocks it at times,
  especially from GitHub's servers; a blocked listing keeps its last good values and
  is marked as such. Once `SP_API_*` secrets are added to the repository
  (Settings → Secrets → Actions), price and sales rank come from SP-API instead.
  SP-API has no review data, so ratings and review counts still come from the page.
- **Sentiment:** every store review (Judge.me, newest first, kept in
  `reviews/<brand>.json.gz` without reviewer names) is tagged with the topics its text
  mentions (fit, fabric, durability, color, …); its stars decide whether it's praise
  or a complaint. "Where the complaints are" names each product's most common topic
  in its 1–2★ reviews. On Amazon, the "Customers say" topics Amazon itself
  computes are read off each listing page.
- **Readings are rules, not forecasts.** Every "What it means" and opportunity is
  computed from the numbers by `src/amz_agents/market/analyze.py`; the same data
  always gives the same text.
