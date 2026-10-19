# Dagacci public snapshot

A one-page dashboard of what anyone can see about Dagacci Medical Uniform online:
catalog, prices, stock and on-site reviews from dagacci.com, plus the brand's
public profile and the marketplaces it sells on. Open `index.html` in a browser.

No credentials are used. The numbers come from two public sources on dagacci.com:

- the store's catalog feed (`/products.json`): titles, prices, compare-at prices,
  color/size options and whether each can be bought
- each product page's review widget (Judge.me): average rating and review count

Amazon and Walmart block automated reading, so their ratings, ranks and prices
aren't included; the agents get those from SP-API once it's connected.

## Refresh

```sh
python dagacci-public/collect.py   # fetch the latest data into snapshot.json (~30 s)
python dagacci-public/build.py     # render index.html from snapshot.json + template.html
```

`snapshot.json` is committed, so the history of prices, stock and reviews builds
up one commit per refresh.
