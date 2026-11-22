#!/usr/bin/env python3
"""
reddit-lk-finance-finder

Read-only discovery tool: finds public subreddits that discuss Sri Lanka's
economy, the Colombo Stock Exchange (CSE) and government gazette notices,
then ranks them by relevance, size and recent activity.

What it does on Reddit (and nothing else):
  1. GET /subreddits/search      - keyword search for public communities
  2. GET /r/{name}/about         - public metadata (members, description, created)
  3. GET /r/{name}/new?limit=25  - dates of the latest public posts (activity check)

It never posts, comments, votes, messages or moderates, and it does not
collect usernames or any other personal data. Results are written to local
files only.

Auth: app-only OAuth (client_credentials) using a Reddit "script" app.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

DEFAULT_KEYWORDS = [
    "Sri Lanka",
    "Sri Lanka economy",
    "Sri Lanka stocks",
    "Sri Lanka investing",
    "Sri Lanka finance",
    "Colombo Stock Exchange",
    "CSE Sri Lanka",
    "Colombo",
    "CBSL",
    "LKR",
    "Sri Lanka gazette",
    "Sri Lanka politics",
    "Lanka",
]

# A subreddit must mention at least one of these to count as Sri Lanka-related.
CONTEXT_TERMS = [
    "sri lanka", "srilanka", "sri lankan", "lanka", "colombo", "ceylon",
    "lkr", "cbsl", "cse", "kandy", "galle", "jaffna",
]

# Topic terms that raise relevance (economy / markets / government notices).
TOPIC_TERMS = {
    "stock": 3, "stocks": 3, "share": 2, "shares": 2, "cse": 3, "invest": 3,
    "investing": 3, "investment": 3, "trading": 2, "market": 2, "finance": 3,
    "financial": 2, "economy": 3, "economic": 3, "economics": 3, "business": 2,
    "bank": 2, "cbsl": 3, "lkr": 3, "rupee": 2, "tax": 2, "budget": 2,
    "gazette": 3, "government": 2, "policy": 2, "politics": 1, "news": 1,
    "money": 2, "crypto": 1,
}


# --------------------------------------------------------------------------- #
# HTTP client with polite rate limiting
# --------------------------------------------------------------------------- #

class RedditClient:
    def __init__(self, client_id: str, client_secret: str, user_agent: str,
                 min_interval: float = 1.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.min_interval = min_interval
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._token: str | None = None
        self._token_expiry = 0.0
        self._last_call = 0.0

    def _auth(self) -> None:
        resp = self.session.post(
            TOKEN_URL,
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"Auth failed: {data}")
        self._token = data["access_token"]
        self._token_expiry = time.time() + data.get("expires_in", 3600) - 60

    def get(self, path: str, params: dict | None = None) -> dict:
        if not self._token or time.time() >= self._token_expiry:
            self._auth()

        # Space out requests.
        wait = self.min_interval - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)

        for attempt in range(3):
            self._last_call = time.time()
            resp = self.session.get(
                API + path,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=20,
            )
            self._respect_rate_limit(resp)
            if resp.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            if resp.status_code in (403, 404):
                return {}  # private, banned or missing subreddit
            resp.raise_for_status()
            return resp.json()
        return {}

    @staticmethod
    def _respect_rate_limit(resp: requests.Response) -> None:
        try:
            remaining = float(resp.headers.get("x-ratelimit-remaining", 100))
            reset = float(resp.headers.get("x-ratelimit-reset", 0))
        except ValueError:
            return
        if remaining < 5 and reset > 0:
            time.sleep(reset + 1)


# --------------------------------------------------------------------------- #
# Data model and scoring
# --------------------------------------------------------------------------- #

@dataclass
class Community:
    name: str
    title: str = ""
    description: str = ""
    subscribers: int = 0
    created_utc: float = 0.0
    matched_queries: set[str] = field(default_factory=set)
    latest_post_utc: float = 0.0
    posts_last_30d: int = 0
    relevance: float = 0.0
    score: float = 0.0

    @property
    def url(self) -> str:
        return f"https://www.reddit.com/r/{self.name}/"

    def text(self) -> str:
        return f"{self.name} {self.title} {self.description}".lower()

    def is_sri_lanka_related(self) -> bool:
        t = self.text()
        return any(term in t for term in CONTEXT_TERMS)


def relevance_score(c: Community) -> float:
    name, title, desc = c.name.lower(), c.title.lower(), c.description.lower()
    score = 0.0
    for term, weight in TOPIC_TERMS.items():
        if term in name:
            score += weight * 3
        elif term in title:
            score += weight * 2
        elif term in desc:
            score += weight
    for term in CONTEXT_TERMS:
        if term in name:
            score += 4
            break
    score += 2 * len(c.matched_queries)  # found by several searches = stronger match
    return score


def days_since(ts: float) -> float:
    if not ts:
        return float("inf")
    return (time.time() - ts) / 86400


def normalise(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def rank(communities: list[Community]) -> list[Community]:
    if not communities:
        return []
    for c in communities:
        c.relevance = relevance_score(c)

    rel = normalise([c.relevance for c in communities])
    size = normalise([math.log10(c.subscribers + 1) for c in communities])
    # Activity: recent posting volume, penalised if the newest post is old.
    act_raw = []
    for c in communities:
        freshness = 1.0 / (1.0 + days_since(c.latest_post_utc) / 7)
        act_raw.append(c.posts_last_30d * 0.5 + freshness * 10)
    act = normalise(act_raw)

    for c, r, s, a in zip(communities, rel, size, act):
        c.score = round(0.45 * r + 0.30 * s + 0.25 * a, 4)
    return sorted(communities, key=lambda c: c.score, reverse=True)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def search(client: RedditClient, keywords: list[str], per_query: int) -> dict[str, Community]:
    found: dict[str, Community] = {}
    for kw in keywords:
        print(f"  search: {kw!r}", file=sys.stderr)
        data = client.get("/subreddits/search",
                          {"q": kw, "limit": per_query, "include_over_18": "false"})
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            if d.get("subreddit_type") not in ("public", "restricted"):
                continue
            name = d.get("display_name", "")
            if not name:
                continue
            c = found.setdefault(name.lower(), Community(name=name))
            c.title = d.get("title") or c.title
            c.description = d.get("public_description") or c.description
            c.subscribers = d.get("subscribers") or c.subscribers
            c.created_utc = d.get("created_utc") or c.created_utc
            c.matched_queries.add(kw)
    return found


def enrich(client: RedditClient, c: Community) -> None:
    about = client.get(f"/r/{c.name}/about").get("data", {})
    if about:
        c.subscribers = about.get("subscribers") or c.subscribers
        c.description = about.get("public_description") or c.description
        c.title = about.get("title") or c.title

    posts = client.get(f"/r/{c.name}/new", {"limit": 25}).get("data", {}).get("children", [])
    cutoff = time.time() - 30 * 86400
    times = [p["data"].get("created_utc", 0) for p in posts if "data" in p]
    if times:
        c.latest_post_utc = max(times)
        c.posts_last_30d = sum(1 for t in times if t >= cutoff)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def fmt_date(ts: float) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def write_outputs(ranked: list[Community], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    csv_path = out_dir / f"subreddits-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "subreddit", "score", "members", "posts_last_30d",
                    "latest_post", "created", "matched_queries", "description", "url"])
        for i, c in enumerate(ranked, 1):
            w.writerow([i, f"r/{c.name}", c.score, c.subscribers, c.posts_last_30d,
                        fmt_date(c.latest_post_utc), fmt_date(c.created_utc),
                        "; ".join(sorted(c.matched_queries)),
                        c.description.replace("\n", " ")[:300], c.url])

    json_path = out_dir / f"subreddits-{stamp}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump([{**asdict(c), "matched_queries": sorted(c.matched_queries), "url": c.url}
                   for c in ranked], f, indent=2, ensure_ascii=False)

    md_path = out_dir / f"subreddits-{stamp}.md"
    with md_path.open("w", encoding="utf-8") as f:
        f.write(f"# Sri Lanka economy / markets subreddits ({stamp})\n\n")
        f.write("| # | Subreddit | Members | Posts (30d) | Latest post | Score |\n")
        f.write("|---|-----------|--------:|------------:|-------------|------:|\n")
        for i, c in enumerate(ranked, 1):
            f.write(f"| {i} | [r/{c.name}]({c.url}) | {c.subscribers:,} | "
                    f"{c.posts_last_30d} | {fmt_date(c.latest_post_utc)} | {c.score} |\n")

    print(f"\nSaved: {csv_path}\n       {json_path}\n       {md_path}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def load_env(path: Path = Path(".env")) -> None:
    """Minimal .env loader so no extra dependency is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    p = argparse.ArgumentParser(description="Find and rank Sri Lanka economy/market subreddits (read-only).")
    p.add_argument("-k", "--keyword", action="append",
                   help="Search keyword (repeatable). Defaults to a built-in Sri Lanka finance list.")
    p.add_argument("--per-query", type=int, default=50, help="Results per keyword search (max 100).")
    p.add_argument("--top", type=int, default=30, help="How many ranked results to keep.")
    p.add_argument("--min-members", type=int, default=50, help="Skip communities smaller than this.")
    p.add_argument("--no-filter", action="store_true",
                   help="Keep results that don't mention Sri Lanka (off by default).")
    p.add_argument("--out", default="results", help="Output folder.")
    args = p.parse_args()

    load_env()
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    username = os.getenv("REDDIT_USERNAME", "unknown")
    if not client_id or not client_secret:
        print("Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (see .env.example).", file=sys.stderr)
        return 1

    user_agent = f"python:reddit-lk-finance-finder:v1.0 (by /u/{username})"
    client = RedditClient(client_id, client_secret, user_agent)

    keywords = args.keyword or DEFAULT_KEYWORDS
    print(f"Searching {len(keywords)} keywords...", file=sys.stderr)
    found = search(client, keywords, min(args.per_query, 100))

    candidates = [c for c in found.values()
                  if c.subscribers >= args.min_members
                  and (args.no_filter or c.is_sri_lanka_related())]
    print(f"{len(found)} communities found, {len(candidates)} relevant. Checking activity...",
          file=sys.stderr)

    for i, c in enumerate(candidates, 1):
        print(f"  [{i}/{len(candidates)}] r/{c.name}", file=sys.stderr)
        enrich(client, c)

    ranked = rank(candidates)[: args.top]

    print(f"\n{'#':>3}  {'subreddit':<28} {'members':>9} {'30d':>4}  {'latest':<10}  score")
    for i, c in enumerate(ranked, 1):
        print(f"{i:>3}  r/{c.name:<26} {c.subscribers:>9,} {c.posts_last_30d:>4}  "
              f"{fmt_date(c.latest_post_utc):<10}  {c.score}")

    write_outputs(ranked, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
