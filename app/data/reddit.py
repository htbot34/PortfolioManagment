"""Reddit social sentiment via the public JSON API.

No auth required. Public sub search endpoint returns recent posts mentioning
the ticker, with score (upvotes) and comment count - rough proxy for retail
attention.
"""
from urllib.parse import quote_plus

import httpx


_HEADERS = {"User-Agent": "PortfolioAdvisor/1.0 (research)"}
_TIMEOUT = 15.0


def ticker_mentions(ticker: str, subreddit: str = "wallstreetbets", limit: int = 10) -> list[dict]:
    """Return recent posts mentioning the ticker in the given sub."""
    url = (f"https://www.reddit.com/r/{subreddit}/search.json"
           f"?q={quote_plus(ticker)}&restrict_sr=1&sort=new&t=week&limit={limit}")
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        data = r.json()
        posts = data.get("data", {}).get("children", [])
    except Exception:
        return []
    out = []
    for p in posts:
        d = p.get("data") or {}
        out.append({
            "title": d.get("title"),
            "score": d.get("score") or 0,
            "comments": d.get("num_comments") or 0,
            "subreddit": d.get("subreddit"),
            "url": "https://reddit.com" + (d.get("permalink") or ""),
        })
    return out


def attention(ticker: str) -> dict:
    """Aggregated attention metric across wsb + stocks + investing."""
    total_posts = 0
    total_score = 0
    total_comments = 0
    top_titles: list[str] = []
    for sub in ("wallstreetbets", "stocks", "investing"):
        posts = ticker_mentions(ticker, sub, limit=10)
        total_posts += len(posts)
        for p in posts:
            total_score += p["score"]
            total_comments += p["comments"]
        if posts:
            top_titles.append(f"{sub}: {posts[0]['title']}")
    return {
        "post_count_7d": total_posts,
        "total_upvotes_7d": total_score,
        "total_comments_7d": total_comments,
        "top_titles": top_titles[:3],
    }
