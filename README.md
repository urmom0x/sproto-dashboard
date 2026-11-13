# Sproto Gremlins Watch

Daily dashboard analyzing the [Sproto Gremlins](https://opensea.io/collection/sproto-gremlins) NFT collection
(Ethereum, contract `0xeeca64ea9fcf99a22806cd99b3d29cf6e8d54925`).

- `collect.py` pulls raw data incrementally into `data/` (Etherscan v2 + OpenSea v2).
- `analyze.py` builds `docs/data.json` and appends a daily snapshot to `data/history.json`.
- `docs/index.html` is the static dashboard (GitHub Pages, served from `/docs`).
- `.github/workflows/daily.yml` runs both daily and commits the results.

## Setup
1. Repo secrets: `OPENSEA_API_KEY`, `ETHERSCAN_API_KEY`.
2. Settings → Pages → Deploy from branch `main`, folder `/docs`.

## Local run
```sh
cp .env.example .env   # fill in keys
set -a; . ./.env; set +a
python collect.py            # or: python collect.py transfers sales stats
python analyze.py
python -m http.server -d docs
```

Tuning knobs (service-funder threshold, airdrop rules, flip/round-trip windows) are constants at the top of `analyze.py`.
