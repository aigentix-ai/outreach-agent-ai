from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

import main as core
import stop_controller


def _extract_domains_from_text(text: str) -> list[str]:
    """Find external business domains mentioned in text or comments."""
    raw_urls = re.findall(r"https?://(?:www\.)?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", text)
    valid = []
    ignored = {"reddit.com", "quora.com", "imgur.com", "google.com", "youtube.com", "wikipedia.org", "twitter.com", "x.com"}
    for u in raw_urls:
        domain = u.lower().removeprefix("www.")
        if domain not in core.SKIP_HOSTS and domain not in ignored and "." in domain:
            if domain not in valid:
                valid.append(domain)
    return valid


def deep_inspect_reddit_comments(thread_url: str, industry: str) -> list[dict]:
    """Fetch live comments from a Reddit thread using clean JSON endpoint."""
    leads = []
    try:
        clean_url = thread_url.split("?")[0].rstrip("/")
        if not clean_url.endswith(".json"):
            clean_url += ".json"
        
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        with httpx.Client(timeout=6.0, headers=headers) as client:
            resp = client.get(clean_url, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 1:
                    comment_data = data[1].get("data", {}).get("children", [])
                    for c in comment_data[:12]:
                        body = c.get("data", {}).get("body", "")
                        author = c.get("data", {}).get("author", "")
                        if not body or author in ["[deleted]", "AutoModerator"]:
                            continue

                        # Check for mentioned businesses or links inside comment
                        domains = _extract_domains_from_text(body)
                        for d in domains:
                            comp = d.split(".")[0].replace("-", " ").title()
                            leads.append({
                                "company": comp,
                                "website": f"https://{d}/",
                                "source": "reddit_comment",
                                "comment_author": author,
                                "text": f"Recommended or discussed in Reddit comment by u/{author}: {body[:250]}",
                            })
    except Exception as exc:
        print(f"[reddit-deep-comment-notice] {exc}")
    return leads


def search_discussions(industry: str, city: str = "", max_results: int = 8) -> list[core.Candidate]:
    """
    Deep Multi-Channel Hunter:
    Searches Reddit, Quora, and review discussions across both main posts and comment threads.
    """
    location_str = f'"{city}"' if city else ""
    queries = [
        f'site:reddit.com inurl:comments "{industry}" {location_str} ("recommend" OR "need a" OR "looking for")',
        f'site:reddit.com inurl:comments "{industry}" {location_str} ("unresponsive" OR "poor service" OR "never answers")',
        f'site:quora.com "{industry}" {location_str} ("recommend" OR "best" OR "complaint")',
        f'"{industry}" {location_str} ("reviews" OR "unresponsive" OR "never answers the phone")',
    ]

    candidates: list[core.Candidate] = []
    seen: set[str] = set()

    for q in queries:
        if stop_controller.is_stop_requested():
            break
        try:
            results = core.fetch_ddg_results(q.strip(), max_results=max_results)
        except Exception as exc:
            print(f"[social-radar-notice] {exc}")
            continue

        for r in results:
            url = r.get("href") or r.get("url") or ""
            title = r.get("title", "")
            snippet = r.get("snippet") or r.get("body") or ""

            # If Reddit thread, perform deep comment inspection
            if "reddit.com" in url and "/comments/" in url:
                comment_leads = deep_inspect_reddit_comments(url, industry)
                for cl in comment_leads:
                    comp_name = cl["company"]
                    if comp_name.lower() in seen:
                        continue
                    seen.add(comp_name.lower())
                    candidates.append(core.Candidate(
                        company=comp_name,
                        website=cl["website"],
                        signal_name="commenter_referral_signal",
                        signal_score=70,
                        evidence_url=url,
                        evidence_text=cl["text"],
                        location=city,
                    ))

            # Direct extraction from search snippet
            domains = _extract_domains_from_text(f"{title} {snippet}")
            valid_company = ""
            valid_website = ""

            for d in domains:
                valid_company = d.split(".")[0].replace("-", " ").title()
                valid_website = f"https://{d}/"
                break

            if not valid_company:
                name_match = re.search(rf"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+{re.escape(industry.title())})\b", f"{title} {snippet}")
                if name_match:
                    valid_company = name_match.group(1).strip()

            if valid_company and valid_company.lower() not in seen:
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
