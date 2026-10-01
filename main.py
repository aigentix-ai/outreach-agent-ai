from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup
from ddgs import DDGS
import ddgs.http_client
import primp

# Ensure DDGS HttpClient works reliably across all primp versions
_orig_ddgs_init = ddgs.http_client.HttpClient.__init__
def _safe_ddgs_init(self, proxy=None, timeout=10, verify=True):
    try:
        _orig_ddgs_init(self, proxy=proxy, timeout=timeout, verify=verify)
    except Exception:
        self.client = primp.Client(proxy=proxy, timeout=timeout, verify=verify)
ddgs.http_client.HttpClient.__init__ = _safe_ddgs_init

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import meta_agent
import social_radar
import stop_controller

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "leads.db"
CONFIG_PATH = ROOT / "config.yaml"
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
COMMON_FREE_EMAILS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}
SKIP_HOSTS = {
    "linkedin.com", "www.linkedin.com", "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com", "tiktok.com", "www.tiktok.com", "pinterest.com", "www.pinterest.com",
    "indeed.com", "www.indeed.com", "glassdoor.com", "www.glassdoor.com", "shine.com", "www.shine.com", "naukri.com", "www.naukri.com",
    "ziprecruiter.com", "www.ziprecruiter.com", "builtin.com", "www.builtin.com", "monster.com", "www.monster.com",
    "wellfound.com", "www.wellfound.com", "lever.co", "greenhouse.io", "workable.com", "dice.com", "careerbuilder.com",
    "yelp.com", "www.yelp.com", "yellowpages.com", "www.yellowpages.com", "mapquest.com", "www.mapquest.com", "reddit.com", "www.reddit.com",
    "quora.com", "www.quora.com", "wikipedia.org", "en.wikipedia.org", "youtube.com", "www.youtube.com", "vimeo.com", "www.vimeo.com",
    "amazon.com", "www.amazon.com", "walmart.com", "www.walmart.com", "target.com", "www.target.com", "ebay.com", "www.ebay.com",
    "aliexpress.com", "www.aliexpress.com", "alibaba.com", "www.alibaba.com", "shein.com", "www.shein.com", "temu.com", "www.temu.com",
    "etsy.com", "www.etsy.com", "sephora.com", "www.sephora.com", "ulta.com", "www.ulta.com", "play.google.com", "apps.apple.com",
    "techcrunch.com", "www.techcrunch.com", "forbes.com", "www.forbes.com", "yourstory.com", "www.yourstory.com",
    "medium.com", "www.medium.com", "substack.com", "www.substack.com", "bloomberg.com", "businessinsider.com",
    "prnewswire.com", "www.prnewswire.com", "businesswire.com", "www.businesswire.com", "reuters.com", "cnn.com", "nytimes.com",
    "trustpilot.com", "sitejabber.com", "bbb.org", "scamadviser.com", "complaintsboard.com",
    "whatsapp.com", "api.whatsapp.com", "wa.me", "t.me", "ghost.org", "msn.com", "zone.msn.com", "support.google.com",
    "crazygames.com", "poki.com", "onlinegames.io",
}


@dataclass
class Candidate:
    company: str
    website: str
    signal_name: str
    signal_score: int
    evidence_url: str
    evidence_text: str
    email: str | None = None
    score: int = 0
    location: str = ""
    whatsapp_number: str = ""
    whatsapp_draft: str = ""
    ad_quality: str = ""
    ad_audit_notes: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        example_path = ROOT / "config.example.yaml"
        if example_path.exists():
            import shutil
            shutil.copy(example_path, CONFIG_PATH)
        else:
            raise FileNotFoundError(f"Missing config file at {CONFIG_PATH} and no config.example.yaml found.")
    content = CONFIG_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(content) or {}


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE NOT NULL,
            company TEXT NOT NULL,
            website TEXT NOT NULL,
            email TEXT,
            signal_name TEXT NOT NULL,
            signal_score INTEGER NOT NULL,
            score INTEGER NOT NULL,
            evidence_url TEXT NOT NULL,
            evidence_text TEXT,
            status TEXT NOT NULL DEFAULT 'discovered',
            subject TEXT,
            body TEXT,
            gmail_message_id TEXT,
            research_text TEXT,
            pain_summary TEXT,
            ai_reason TEXT,
            ai_confidence INTEGER DEFAULT 0,
            ai_evidence_quote TEXT,
            contact_name TEXT,
            contact_title TEXT,
            contact_source_url TEXT,
            contact_source_text TEXT,
            contact_confidence INTEGER DEFAULT 0,
            contact_direct_match INTEGER DEFAULT 0,
            email_mx_valid INTEGER DEFAULT 0,
            rating INTEGER DEFAULT 0,
            feedback_note TEXT,
            location TEXT,
            whatsapp_number TEXT,
            whatsapp_draft TEXT,
            ad_quality TEXT,
            ad_audit_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suppressions (
            email TEXT PRIMARY KEY,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Migration safeguard for existing databases missing extra columns
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    extra_cols = {
        "research_text": "TEXT",
        "pain_summary": "TEXT",
        "ai_reason": "TEXT",
        "ai_confidence": "INTEGER DEFAULT 0",
        "ai_evidence_quote": "TEXT",
        "contact_name": "TEXT",
        "contact_title": "TEXT",
        "contact_source_url": "TEXT",
        "contact_source_text": "TEXT",
        "contact_confidence": "INTEGER DEFAULT 0",
        "contact_direct_match": "INTEGER DEFAULT 0",
        "email_mx_valid": "INTEGER DEFAULT 0",
        "rating": "INTEGER DEFAULT 0",
        "feedback_note": "TEXT",
        "location": "TEXT",
        "whatsapp_number": "TEXT",
        "whatsapp_draft": "TEXT",
        "ad_quality": "TEXT",
        "ad_audit_notes": "TEXT",
        "needs_clarification": "INTEGER DEFAULT 0",
        "clarification_question": "TEXT",
    }
    for col_name, col_type in extra_cols.items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col_name} {col_type}")
    conn.commit()
    return conn


def normalize_url(url: str) -> str:
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def root_url(url: str) -> str:
    p = urlparse(normalize_url(url))
    return f"{p.scheme}://{p.netloc}/"


def company_from_result(title: str, url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    fallback = host.split(".")[0].replace("-", " ").title()
    title = re.sub(r"\s+[|\-–—:]\s+.*$", "", title or "").strip()
    if not title or len(title) > 35 or any(w in title.lower() for w in ["buy ", "shop ", "evolution", "official store", "home", "welcome", "best ", "cart"]):
        return fallback
    return title


def fingerprint(company: str, website: str, email_addr: str | None) -> str:
    raw = f"{company.lower().strip()}|{root_url(website).lower()}|{(email_addr or '').lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


JUNK_DOMAINS = {
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "builtin.com", "monster.com",
    "wellfound.com", "lever.co", "greenhouse.io", "workable.com", "jobserve.com",
    "dice.com", "careerbuilder.com", "simplyhired.com", "theladders.com",
    "prnewswire.com", "businesswire.com", "globenewswire.com", "reuters.com",
    "bloomberg.com", "forbes.com", "techcrunch.com", "medium.com", "substack.com",
    "cnn.com", "nytimes.com", "theguardian.com", "bbc.com", "theverge.com",
    "trustpilot.com", "sitejabber.com", "bbb.org", "scamadviser.com", "complaintsboard.com",
    "wikipedia.org", "wiktionary.org", "wikihow.com", "investopedia.com", "dictionary.com",
    "amazon.com", "walmart.com", "target.com", "ebay.com", "aliexpress.com", "alibaba.com",
    "shein.com", "temu.com", "dhgate.com",
}

JUNK_PATTERNS = [
    r"\bapply (now )?for this (job|position|role)\b",
    r"\bjob description\b",
    r"\bsalary range\b",
    r"\byears of experience required\b",
    r"\bsubmit your resume\b",
    r"\bopen positions\b",
    r"\bhiring manager\b",
    r"\brecruiter\b",
    r"\bpress release\b",
    r"\bbreaking news\b",
    r"\bstaff writer\b",
    r"\beditor-in-chief\b",
    r"\ball rights reserved news\b",
    r"\bscam alert\b",
    r"\bcomplaints against\b",
    r"\bripoff report\b",
    r"\bfraud warning\b",
]


def is_irrelevant_junk(company: str, url: str, text: str) -> tuple[bool, str]:
    domain = urlparse(normalize_url(url)).netloc.lower().removeprefix("www.")
    for jd in JUNK_DOMAINS:
        if jd in domain or domain.endswith("." + jd):
            return True, f"irrelevant junk/portal domain: {jd}"

    comp_lower = company.lower()
    for jk in ["job", "jobs", "career", "careers", "hiring", "news", "times", "press", "journal", "magazine", "scam", "complaint", "review", "directory"]:
        if re.search(r"\b" + re.escape(jk) + r"\b", comp_lower):
            return True, f"company name indicator: '{jk}'"

    text_lower = text.lower() if text else ""
    for pat in JUNK_PATTERNS:
        if re.search(pat, text_lower):
            return True, f"detected junk content pattern"

    return False, ""


def check_blacklist(candidate_name: str, url: str, text: str, config: dict) -> tuple[bool, str]:
    # 1. Pre-check against built-in junk/scam/news filters
    is_junk, junk_reason = is_irrelevant_junk(candidate_name, url, text)
    if is_junk:
        return True, junk_reason

    filters = config.get("filters", {})
    keywords = [k.lower().strip() for k in filters.get("blacklist_keywords", []) if k.strip()]
    domains = [d.lower().strip() for d in filters.get("blacklist_domains", []) if d.strip()]

    parsed = urlparse(normalize_url(url))
    domain = parsed.netloc.lower().removeprefix("www.")

    for bd in domains:
        if bd in domain:
            return True, f"blacklisted domain: {bd}"

    name_and_text = f"{candidate_name} {text}".lower()
    for kw in keywords:
        pattern = r"\b" + re.escape(kw) + r"\b"
        if re.search(pattern, name_and_text):
            return True, f"blacklisted keyword: '{kw}'"

    return False, ""


def get_channel_config(config: dict, channel_name: str | None = None) -> tuple[str, dict]:
    channels_cfg = config.get("channels", {})
    active = channel_name or channels_cfg.get("active_channel", "default_gmail")
    accounts = channels_cfg.get("accounts", {})
    account = accounts.get(active, {})
    return active, account


STEALTH_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
]

STEALTH_REFERRERS = [
    "https://www.google.com/",
    "https://www.bing.com/",
    "https://duckduckgo.com/",
    "https://search.yahoo.com/",
]


def get_stealth_headers(custom_ua: str | None = None) -> dict[str, str]:
    ua = custom_ua or random.choice(STEALTH_USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": random.choice(STEALTH_REFERRERS),
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
    }
    if "Chrome" in ua or "Edg" in ua:
        headers["sec-ch-ua"] = '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"'
        headers["sec-ch-ua-mobile"] = "?0"
        headers["sec-ch-ua-platform"] = '"Windows"' if "Windows" in ua else '"macOS"'
    return headers


def human_delay(min_sec: float = 1.0, max_sec: float = 2.5) -> None:
    """Random human-like pacing delay to avoid burst rate limits."""
    time.sleep(random.uniform(min_sec, max_sec))


def fetch_ddg_results(query: str, max_results: int = 15) -> list[dict]:
    results = []
    headers = get_stealth_headers()
    try:
        with httpx.Client(timeout=12.0, headers=headers) as client:
            resp = client.post("https://html.duckduckgo.com/html/", data={"q": query}, follow_redirects=True)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for row in soup.find_all("div", class_="result"):
                    t_elem = row.find("a", class_="result__a") or row.find("a", class_="result__title") or row.find("a", class_="result__url")
                    s_elem = row.find("a", class_="result__snippet") or row.find("div", class_="result__snippet")
                    u_elem = row.find("a", class_="result__a") or row.find("a", class_="result__url")
                    if not t_elem:
                        continue
                    href = u_elem.get("href") if u_elem else t_elem.get("href")
                    if href and "uddg=" in href:
                        import urllib.parse
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        if "uddg" in qs:
                            href = qs["uddg"][0]
                    title = t_elem.get_text(strip=True)
                    snippet = s_elem.get_text(strip=True) if s_elem else ""
                    if href and href.startswith("http"):
                        results.append({"title": title, "href": href, "snippet": snippet})
                        if len(results) >= max_results:
                            break
    except Exception as exc:
        print(f"[ddg-html-notice] {exc}")

    if not results:
        try:
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    href = r.get("href") or r.get("url") or ""
                    if href and href.startswith("http"):
                        results.append({
                            "title": r.get("title", ""),
                            "href": href,
                            "snippet": r.get("body") or r.get("snippet") or "",
                        })
        except Exception as exc:
            print(f"[ddgs-lib-notice] {exc}")
    return results


def extract_brands_from_listicle(url: str, html: str, skip_hosts: set[str], max_brands: int = 8) -> list[tuple[str, str]]:
    """If a result is an editorial listicle/directory, extract cited brand domains."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(url).netloc.lower().removeprefix("www.")
    extracted: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        p = urlparse(href)
        domain = p.netloc.lower().removeprefix("www.")
        if not domain or "." not in domain:
            continue
        if domain == base_domain or domain.endswith("." + base_domain):
            continue
        if domain in skip_hosts or any(sh in domain for sh in skip_hosts):
            continue
        if any(ign in domain for ign in ["apple.com", "google.com", "w3.org", "cloudflare.com", "shopify.com", "wordpress.org", "schema.org"]):
            continue
        if domain in seen:
            continue

        seen.add(domain)
        text = a.get_text(strip=True)
        name = text if (2 <= len(text) <= 40 and not text.startswith("http")) else domain.split(".")[0].replace("-", " ").title()
        brand_url = f"{p.scheme}://{p.netloc}/"
        extracted.append((name, brand_url))
        if len(extracted) >= max_brands:
            break

    return extracted


def search_candidates(config: dict, reporter=None) -> list[Candidate]:
    discovery = config.get("discovery", {})
    sources = discovery.get("sources", {})
    # DuckDuckGo is enabled by default unless explicitly disabled
    use_ddg = sources.get("duckduckgo", True)
    use_meta = sources.get("meta_ads", True)
    use_csv = sources.get("csv_import", False)
    use_social = sources.get("social_radar", False) or sources.get("reddit_quora", False)

    target = config.get("target", {})
    filters = config.get("filters", {})
    excluded_industries = {i.lower() for i in filters.get("excluded_industries", [])}
    max_results = int(target.get("max_search_results_per_query", 10))
    countries = target.get("countries", [""])
    cities = target.get("cities", [""])
    if not cities:
        cities = [""]
    industries = [ind for ind in target.get("industries", []) if ind.lower() not in excluded_industries]
    signals = config.get("signals", [])
    found: list[Candidate] = []
    seen_websites: set[str] = set()

    if stop_controller.is_stop_requested(config):
        print("[STOP] Search aborted: Emergency Kill Switch is active.")
        return found

    # 1. Direct CSV list import
    if use_csv:
        csv_path = discovery.get("csv_path", "leads_input.csv")
        csv_leads = meta_agent.import_csv_leads(csv_path)
        for c in csv_leads:
            if not c.website and c.company:
                c.website = meta_agent.resolve_company_website(c.company)
            if c.website:
                site_root = root_url(c.website)
                if site_root in seen_websites:
                    continue
                is_blocked, reason = check_blacklist(c.company, c.website, c.evidence_text, config)
                if is_blocked:
                    if reporter:
                        reporter.log_blacklist(c.company, reason)
                    continue
                seen_websites.add(site_root)
                if reporter:
                    reporter.log_scan(1)
                found.append(c)
                print(f"[csv-candidate] {c.company}: {site_root} ({c.signal_name})", flush=True)
            elif c.email:
                dedup_key = f"email:{c.email.lower()}"
                if dedup_key in seen_websites:
                    continue
                is_blocked, reason = check_blacklist(c.company, "", c.evidence_text, config)
                if is_blocked:
                    if reporter:
                        reporter.log_blacklist(c.company, reason)
                    continue
                seen_websites.add(dedup_key)
                if reporter:
                    reporter.log_scan(1)
                found.append(c)
                print(f"[csv-candidate] {c.company}: {c.email} ({c.signal_name})", flush=True)

    # 2. Meta Ads Library & Facebook Pages
    if use_meta:
        meta_candidates = meta_agent.search_meta_candidates(config)
        for c in meta_candidates:
            if not c.website and c.company:
                c.website = meta_agent.resolve_company_website(c.company)
            if c.website:
                site_root = root_url(c.website)
                if site_root in seen_websites:
                    continue
                is_blocked, reason = check_blacklist(c.company, c.website, c.evidence_text, config)
                if is_blocked:
                    if reporter:
                        reporter.log_blacklist(c.company, reason)
                    continue
                seen_websites.add(site_root)
                if reporter:
                    reporter.log_scan(1)
                found.append(c)
                print(f"[meta-candidate] {c.company}: {site_root} ({c.signal_name})", flush=True)

    # 3. Social Discussion / Comment Radar (Reddit, Quora, Forums)
    if use_social:
        for ind in industries:
            for city in cities:
                social_leads = social_radar.search_discussions(ind, city=city, max_results=5)
                for c in social_leads:
                    if not c.website and c.company:
                        c.website = meta_agent.resolve_company_website(c.company)
                    if c.website:
                        site_root = root_url(c.website)
                        if site_root in seen_websites:
                            continue
                        is_blocked, reason = check_blacklist(c.company, c.website, c.evidence_text, config)
                        if is_blocked:
                            continue
                        seen_websites.add(site_root)
                        if reporter:
                            reporter.log_scan(1)
                        found.append(c)
                        print(f"[social-candidate] {c.company}: {site_root} ({c.signal_name})", flush=True)

    # 4. DuckDuckGo Search (optional)
    if use_ddg:
        for industry in industries:
            for city in cities:
                for signal in signals:
                    for pattern in signal.get("query_patterns", []):
                        queries_to_run = []
                        for c in countries:
                            q = pattern.format(industry=industry, country=c)
                            if city:
                                q = f"{q} {city}"
                            queries_to_run.append(q.strip())
                        for q in queries_to_run:
                            print(f"[search] Searching: {q}")
                            results = fetch_ddg_results(q, max_results=max_results)
                            for r in results:
                                url = normalize_url(r.get("href") or "")
                                if not url:
                                    continue
                                host = urlparse(url).netloc.lower().removeprefix("www.")
                                if host in SKIP_HOSTS or any(sh in host for sh in SKIP_HOSTS):
                                    continue

                                company = company_from_result(r.get("title", ""), url)
                                body_text = (r.get("snippet") or "")[:1000]

                                # Check if this result is a listicle or directory article
                                is_listicle = any(w in url.lower() for w in ["/blog/", "/article/", "/top-", "/best-", "brands-in"]) or \
                                              any(w in r.get("title", "").lower() for w in ["top 10", "top 20", "top 50", "best d2c", "brands you"])

                                if is_listicle:
                                    try:
                                        with httpx.Client(timeout=4.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
                                            art_html = fetch_html(client, url)
                                            if art_html:
                                                brand_links = extract_brands_from_listicle(url, art_html, SKIP_HOSTS)
                                                for b_name, b_url in brand_links:
                                                    b_root = root_url(b_url)
                                                    if b_root in seen_websites:
                                                        continue
                                                    is_blocked, _ = check_blacklist(b_name, b_url, "", config)
                                                    if is_blocked:
                                                        continue
                                                    seen_websites.add(b_root)
                                                    if reporter:
                                                        reporter.log_scan(1)
                                                    clean_b_name = b_name.encode("ascii", "ignore").decode("ascii")
                                                    print(f"[brand-from-listicle] {clean_b_name}: {b_root}", flush=True)
                                                    found.append(Candidate(
                                                        company=b_name,
                                                        website=b_root,
                                                        signal_name="curated_d2c_listicle",
                                                        signal_score=50,
                                                        evidence_url=url,
                                                        evidence_text=f"Featured in curated list of top D2C brands: {r.get('title', '')}",
                                                    ))
                                    except Exception as exc:
                                        print(f"[listicle-extract-error] {url}: {exc}", flush=True)
                                    continue

                                # Direct brand candidate
                                site_root = root_url(url)
                                if site_root in seen_websites:
                                    continue

                                is_blocked, reason = check_blacklist(company, url, body_text, config)
                                if is_blocked:
                                    if reporter:
                                        reporter.log_blacklist(company, reason)
                                    continue

                                seen_websites.add(site_root)
                                if reporter:
                                    reporter.log_scan(1)
                                clean_comp = company.encode("ascii", "ignore").decode("ascii")
                                print(f"[direct-brand] {clean_comp}: {site_root}", flush=True)

                                found.append(Candidate(
                                    company=company,
                                    website=site_root,
                                    signal_name=signal["name"],
                                    signal_score=int(signal.get("score", 0)),
                                    evidence_url=url,
                                    evidence_text=body_text,
                                ))
                        time.sleep(0.2)
    return found


def fetch_html(client: httpx.Client, url: str, max_retries: int = 2) -> str:
    if stop_controller.is_stop_requested():
        return ""
    for attempt in range(max_retries + 1):
        if stop_controller.is_stop_requested():
            return ""
        try:
            headers = get_stealth_headers()
            r = client.get(url, headers=headers, follow_redirects=True)
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "")
                if "text/html" in ctype or "text/plain" in ctype:
                    return r.text[:1_500_000]
            elif r.status_code in (429, 503):
                # Exponential backoff on rate limits
                time.sleep(1.5 * (attempt + 1))
                continue
            elif r.status_code >= 400:
                return ""
        except Exception:
            if attempt < max_retries:
                time.sleep(1.0)
            else:
                return ""
    return ""


def extract_public_emails(html: str, website: str) -> list[str]:
    emails = {e.lower().strip(".,;:()[]{}<>\"'") for e in EMAIL_RE.findall(html or "")}
    host = urlparse(website).netloc.lower().removeprefix("www.")
    good = []
    for e in emails:
        domain = e.split("@")[-1]
        if any(x in e for x in ["example.com", "sentry.io", "wixpress.com", "cloudflare.com"]):
            continue
        if domain == host or domain.endswith("." + host) or domain not in COMMON_FREE_EMAILS:
            good.append(e)
    priority = ["hello@", "info@", "contact@", "sales@", "office@", "admin@", "support@"]
    good.sort(key=lambda e: next((i for i, p in enumerate(priority) if e.startswith(p)), 99))
    return good


def research_candidate(candidate: Candidate, config: dict) -> Candidate:
    crawler = config.get("crawler", {})
    timeout = int(crawler.get("timeout_seconds", 12))
    max_pages = int(crawler.get("max_pages_per_domain", 5))
    ua = crawler.get("user_agent", "Mozilla/5.0")
    pages = ["/", "/contact", "/contact-us", "/about", "/careers", "/jobs"][:max_pages]
    texts: list[str] = []
    emails: list[str] = []

    with httpx.Client(timeout=timeout, headers={"User-Agent": ua}) as client:
        for path in pages:
            url = urljoin(candidate.website, path)
            html = fetch_html(client, url)
            if not html:
                continue
            emails.extend(extract_public_emails(html, candidate.website))
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = " ".join(soup.stripped_strings)
            texts.append(text[:12000])

    if not candidate.email and emails:
        candidate.email = emails[0]
    corpus = " ".join(texts).lower()
    bonus = 0
    signal_terms = {
        "hiring_receptionist": ["receptionist", "front desk", "office coordinator", "customer service representative"],
        "unanswered_calls": ["call us", "phone", "24/7", "after hours"],
        "appointment_friction": ["call to schedule", "call for appointment", "book by phone"],
    }
    for term in signal_terms.get(candidate.signal_name, []):
        if term in corpus:
            bonus += 5
    candidate.score = min(100, candidate.signal_score + bonus)
    return candidate


def save_candidate(conn: sqlite3.Connection, c: Candidate) -> bool:
    fp = fingerprint(c.company, c.website, c.email)
    try:
        conn.execute(
            """INSERT INTO leads
            (fingerprint, company, website, email, signal_name, signal_score, score, evidence_url, evidence_text, status, location, whatsapp_number, whatsapp_draft, ad_quality, ad_audit_notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, ?, ?, ?, ?, ?)""",
            (fp, c.company, c.website, c.email, c.signal_name, c.signal_score, c.score, c.evidence_url, c.evidence_text, c.location, c.whatsapp_number, c.whatsapp_draft, c.ad_quality, c.ad_audit_notes, now_iso(), now_iso()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def deterministic_email(row: sqlite3.Row, config: dict) -> tuple[str, str]:
    offer = config.get("offer", {})
    outreach = config.get("outreach", {})
    company_name = row["company"] if "company" in row.keys() else "your business"
    signal = row["signal_name"].replace("_", " ") if "signal_name" in row.keys() and row["signal_name"] else "growth"
    subject = outreach.get("subject_template", "Quick question about {company}").format(company=company_name)
    evidence = (row["evidence_text"] or "").strip() if "evidence_text" in row.keys() and row["evidence_text"] else ""
    if evidence:
        opener = f"I came across {company_name} while looking at businesses showing a possible {signal} signal."
    else:
        opener = f"I came across {company_name} and noticed a possible {signal} signal."
    offer_desc = offer.get("description", "delivering high-impact growth and AI solutions.")
    cta = offer.get("cta", "Open to a quick chat?")
    sender = offer.get("sender_name", "")
    body = (
        f"Hi,\n\n{opener}\n\n"
        f"I help businesses with {offer_desc}\n\n"
        f"{cta}\n\n"
        f"Best,\n{sender}"
    )
    if outreach.get("include_opt_out", True):
        body += "\n\nP.S. If this isn't relevant, reply 'no' and I won't follow up."
    return subject, body


def gmail_service(credentials_path: str | Path | None = None, token_path: str | Path | None = None) -> object:
    load_dotenv(ROOT / ".env")
    c_env = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    t_env = os.getenv("GMAIL_TOKEN_FILE", "token.json")

    creds_file = Path(credentials_path) if credentials_path else Path(c_env)
    token_file = Path(token_path) if token_path else Path(t_env)

    if not creds_file.is_absolute():
        creds_file = ROOT / creds_file
    if not token_file.is_absolute():
        token_file = ROOT / token_file

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not creds_file.exists():
            raise FileNotFoundError(f"Missing Gmail OAuth file: {creds_file}. Set up credentials.json to enable Gmail drafting/sending.")
        flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def build_gmail_message(to: str, subject: str, body: str) -> dict:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to
    msg["subject"] = subject
    msg["date"] = email.utils.formatdate(localtime=True)
    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"raw": encoded}


def outreach(conn: sqlite3.Connection, config: dict) -> None:
    conn.row_factory = sqlite3.Row
    min_score = int(config.get("qualification", {}).get("min_score", 35))
    require_email = bool(config.get("qualification", {}).get("require_public_email", True))
    cap = int(config.get("outreach", {}).get("daily_cap", 25))
    mode = config.get("outreach", {}).get("send_mode", "draft").lower()

    rows = conn.execute(
        """SELECT * FROM leads
           WHERE status='discovered' AND score >= ?
           ORDER BY score DESC, created_at ASC LIMIT ?""",
        (min_score, cap),
    ).fetchall()

    if not rows:
        print("No qualified undispatched leads.")
        return

    service = gmail_service()
    count = 0
    for row in rows:
        if require_email and not row["email"]:
            continue
        if not row["email"]:
            continue
        suppressed = conn.execute("SELECT 1 FROM suppressions WHERE email=?", (row["email"],)).fetchone()
        if suppressed:
            continue

        subject, body = deterministic_email(row, config)
        payload = {"message": build_gmail_message(row["email"], subject, body)}
        if mode == "send":
            result = service.users().messages().send(userId="me", body=payload["message"]).execute()
            status = "sent"
        else:
            result = service.users().drafts().create(userId="me", body=payload).execute()
            status = "drafted"

        conn.execute(
            "UPDATE leads SET status=?, subject=?, body=?, gmail_message_id=?, updated_at=? WHERE id=?",
            (status, subject, body, result.get("id", ""), now_iso(), row["id"]),
        )
        conn.commit()
        count += 1
        print(f"[{status}] {row['company']} <{row['email']}> score={row['score']}")
        time.sleep(0.3)
    print(f"Completed: {count} {mode}(s).")


def discover(conn: sqlite3.Connection, config: dict) -> None:
    candidates = search_candidates(config)
    print(f"Search produced {len(candidates)} candidate result(s). Researching...")
    created = 0
    min_score = int(config.get("qualification", {}).get("min_score", 35))
    require_email = bool(config.get("qualification", {}).get("require_public_email", True))

    for i, c in enumerate(candidates, 1):
        c = research_candidate(c, config)
        if c.score < min_score:
            continue
        if require_email and not c.email:
            continue
        if save_candidate(conn, c):
            created += 1
            print(f"[{created}] {c.company} | {c.email or '-'} | score={c.score} | {c.signal_name}")
        if i % 20 == 0:
            time.sleep(0.5)
    print(f"Saved {created} new qualified lead(s).")


def stats(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT status, COUNT(*) FROM leads GROUP BY status ORDER BY status").fetchall()
    total = sum(r[1] for r in rows)
    print(f"Total leads: {total}")
    for status, count in rows:
        print(f"  {status}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Intent-first agentic outreach engine")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--outreach", action="store_true")
    parser.add_argument("--gmail-auth", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()

    config = load_config()
    conn = init_db()

    if args.gmail_auth:
        gmail_service()
        print("Gmail OAuth ready.")
    elif args.discover:
        discover(conn, config)
    elif args.outreach:
        outreach(conn, config)
    elif args.stats:
        stats(conn)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
