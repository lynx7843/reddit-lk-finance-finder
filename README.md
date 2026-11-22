# reddit-lk-finance-finder

A small, **read-only** Python tool that finds public subreddits discussing
Sri Lanka's economy, the Colombo Stock Exchange (CSE) and government gazette
notices, and ranks them by relevance, size and recent activity.

## What it does

| Step | Endpoint | Purpose |
|------|----------|---------|
| 1 | `GET /subreddits/search` | Keyword search for public communities |
| 2 | `GET /r/{name}/about` | Public metadata: members, description, creation date |
| 3 | `GET /r/{name}/new?limit=25` | Dates of the latest public posts, to measure activity |

Each community gets a score:

- **Relevance (45%)**: Sri Lanka and finance/economy/gazette terms in the name, title and description, plus how many separate searches found it
- **Size (30%)**: member count (log scale)
- **Activity (25%)**: posts in the last 30 days and how recent the newest post is

Communities that don't mention Sri Lanka are dropped (use `--no-filter` to keep them).

## What it does *not* do

- No posting, commenting, voting, messaging or moderation
- No collection of usernames or other personal data, since only post timestamps are read
- No sharing or reselling of data, and no use for AI/ML training
- Low volume: about 1 request per second, a few hundred requests per run, run manually

## Setup

```bash
git clone https://github.com/lynx7843/reddit-lk-finance-finder.git
cd reddit-lk-finance-finder
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # then add your Reddit "script" app credentials
```

## Usage

```bash
python finder.py                                  # default Sri Lanka finance keywords
python finder.py -k "Colombo Stock Exchange" -k "CBSL" --top 20
python finder.py --min-members 500 --out results
```

Results are printed as a table and saved to `results/` as CSV, JSON and Markdown.

<br/>