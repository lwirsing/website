# Monarch Budget & Bill Planner

Streamlit app for monthly Monarch CSV imports, budget reviews, trend analysis, recurring/subscription detection, and bill forecasting.

## What it does

- Imports one or more Monarch transaction CSV files and deduplicates rows.
- Stores all data locally in `finance_data.db`.
- Runs month-by-month category budget reviews (`Budget vs Actual`, variance, over/under status).
- Pre-fills next month budgets from current month actuals and lets you edit/save.
- Visualizes category spend trends over time.
- Tracks one-time and recurring bills (`weekly`, `monthly`, `yearly`) and forecasts upcoming due amounts.
- Detects likely recurring purchases and subscriptions from transaction history.
- Provides a ChatGPT-powered spend-reduction planner (optional; API key required).
- Tracks 2026 category-by-month trends and reduction runway toward a monthly savings goal.

## Setup

```bash
cd "/Users/lwirsing/Documents/New project"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

## Optional: enable AI recommendations tab

```bash
export OPENAI_API_KEY="your_api_key_here"
```

## Data notes

- Monarch import expects columns like: `Date`, `Merchant`, `Category`, `Account`, `Amount`, etc.
- Expenses are treated as negative amounts; trend/review views display spend as positive dollars.
- Transfers can be excluded from monthly budget review.
- Recurring detection is heuristic-based and should be reviewed before canceling/changing services.

## Persistence

- Transactions, budgets, and bills are saved in:
  - `finance_data.db`

---

## Rhode Island Home Commute + Beach Explorer

A separate Streamlit app is included to compare candidate home addresses by:
- Commute time to/from office (`200 Callahan Rd, North Kingstown, RI 02852`)
- Rush-hour vs off-peak drive times (Google traffic estimates)
- Distance and estimated drive time to popular Rhode Island beaches
- Interactive map of selected home, office, and beaches

### Run

```bash
cd "/Users/lwirsing/Documents/New project"
source .venv/bin/activate
pip install -r requirements.txt
streamlit run home_commute_app.py
```

### Google Maps setup

Enable these APIs in Google Cloud and use an API key:
- Geocoding API
- Distance Matrix API

Then set key via one of these options:

```bash
export GOOGLE_MAPS_API_KEY="your_key_here"
```

Or place in Streamlit secrets:

```toml
# .streamlit/secrets.toml
GOOGLE_MAPS_API_KEY = "your_key_here"
```

---

## Deploy As A Website + Use Your Domain

This project now includes production deployment files for Render:
- `Dockerfile`
- `render.yaml`
- `.streamlit/config.toml`

### 1. Push this project to GitHub

```bash
cd "/Users/lwirsing/Documents/New project"
git init
git add .
git commit -m "Initial RI commute explorer site"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

### 2. Deploy on Render

1. Go to [Render Dashboard](https://dashboard.render.com/)
2. Click **New +** -> **Blueprint**
3. Select your GitHub repo
4. Render will read `render.yaml` and create service `ri-home-commute-explorer`
5. In service settings, set env var:
   - `GOOGLE_MAPS_API_KEY` = your key
6. Deploy

After deploy, you will get a Render URL like:
- `https://ri-home-commute-explorer.onrender.com`

### 3. Connect your purchased domain

In Render service:
1. Open **Settings** -> **Custom Domains**
2. Add your domain (for example `homes.yourdomain.com`)
3. Render will show required DNS records

At your domain registrar, create records exactly as shown by Render (usually one of):
- `CNAME` for subdomain (`homes` -> `your-app.onrender.com`)
- `A`/`ALIAS` for apex domain (`yourdomain.com`)

### 4. Wait for DNS + SSL

- DNS propagation can take a few minutes up to 24 hours.
- Render provisions SSL automatically once DNS is correct.

Then your app will be live on your domain.

### Notes

- Keep `GOOGLE_MAPS_API_KEY` in Render environment variables, not in code.
- Restrict your Google API key by API (`Geocoding API`, `Distance Matrix API`) and by allowed origin/IP where practical.
