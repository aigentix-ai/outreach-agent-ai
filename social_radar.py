from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from ddgs import DDGS
import httpx

import main as core


def search_discussions(industry: str, city: str = "", max_results: int = 8) -> list[core.Candidate]:
    """
    Search Reddit, Quora, and forums for active discussions and comments
    where people or business owners discuss problems, recommendations, or needs.
    """
    location_str = f'"{city}"' if city else ""
    queries = [
        f'site:reddit.com inurl:comments "{industry}" {location_str} ("recommend" OR "need a" OR "looking for")',
        f'site:reddit.com inurl:comments "{industry}" {location_str} ("unresponsive" OR "poor service" OR "never answers")',
        f'site:quora.com "{industry}" {location_str} ("recommend" OR "best" OR "complaint")',
    ]

    candidates: list[core.Candidate] = []
    seen: set[str] = set()

    for q in queries:
        try:
            results = core.fetch_ddg_results(q.strip(), max_results=max_results)
        except Exception as exc:
            print(f"[social-radar-notice] {exc}")
            continue

        for r in results:
            url = r.get("href") or r.get("url") or ""
            title = r.get("title", "")
            snippet = r.get("snippet") or r.get("body") or ""

            # Check if an external company or service domain is cited in snippet or thread
            found_domains = re.findall(r"https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", snippet)
            valid_company = ""
            valid_website = ""

            for d in found_domains:
                d_clean = d.lower().removeprefix("www.")
                if d_clean not in core.SKIP_HOSTS and "." in d_clean and not any(ign in d_clean for ign in ["reddit.com", "quora.com", "imgur.com"]):
                    valid_company = d_clean.split(".")[0].replace("-", " ").title()
                    valid_website = f"https://{d_clean}/"
                    break

            # If no direct link, extract company name pattern like "XYZ Plumbing" or use thread context
            if not valid_company:
                name_match = re.search(rf"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+{re.escape(industry.title())})\b", f"{title} {snippet}")
                if name_match:
                    valid_company = name_match.group(1).strip()
                elif "reddit.com" in url:
                    valid_company = f"Reddit Lead ({industry.title()})"
                else:
                    valid_company = f"Discussion Lead ({industry.title()})"

            if valid_company.lower() in seen:
                continue
            seen.add(valid_company.lower())

            candidates.append(core.Candidate(
                company=valid_company,
                website=valid_website,
                signal_name="social_comment_discussion",
                signal_score=65,
                evidence_url=url,
                evidence_text=f"Discussion / Comment Signal: {title} | {snippet[:400]}",
                location=city,
            ))

    return candidates
