from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

import main as core
import stop_controller

ROOT = Path(__file__).resolve().parent


def _extract_external_website(fb_url: str, html_or_text: str) -> str:
    """Extract official company website linked inside a Facebook page or snippet."""
    # Look for Facebook redirect links: l.facebook.com/l.php?u=https%3A%2F%2Fexample.com
    redirect_matches = re.findall(r"l\.facebook\.com/l\.php\?u=([^&\"'\s]+)", html_or_text)
    for rm in redirect_matches:
        decoded = unquote(rm)
        if decoded.startswith(("http://", "https://")):
            p = urlparse(decoded)
            domain = p.netloc.lower().removeprefix("www.")
            if domain and domain not in core.SKIP_HOSTS and "." in domain:
                return f"{p.scheme}://{p.netloc}/"

    # Look for raw external URLs in text
    urls = re.findall(r"https?://[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}[^\s\"'<>]*", html_or_text)
    for u in urls:
        p = urlparse(u)
        domain = p.netloc.lower().removeprefix("www.")
        if domain and domain not in core.SKIP_HOSTS and not domain.endswith("facebook.com") and "." in domain:
            return f"{p.scheme}://{p.netloc}/"

    return ""


def _extract_whatsapp(text: str) -> str:
    """Extract WhatsApp phone number or wa.me link."""
    m = re.search(r"wa\.me/(?:send\?phone=)?(\+?[0-9]{8,15})", text, re.I)
    if m:
        return m.group(1).lstrip("+")
    m2 = re.search(r"whatsapp\.com/send\?phone=(\+?[0-9]{8,15})", text, re.I)
    if m2:
        return m2.group(1).lstrip("+")
    m3 = re.search(r"whatsapp[^\d+]{0,15}(\+?[0-9\s().-]{8,20})", text, re.I)
    if m3:
        clean = re.sub(r"[^\d+]", "", m3.group(1)).lstrip("+")
        if 8 <= len(clean) <= 15:
            return clean
    return ""


def audit_ad_quality(ad_text: str) -> tuple[str, str]:
    """Audit ad creative to find static, low quality, or weak AI ad angles."""
    t = (ad_text or "").lower()
    if not t or len(t) < 40:
        return "minimal_static_ad", "Short static ad text; lacks compelling hook or clear benefit."
    if any(phrase in t for phrase in ["call today", "satisfaction guaranteed", "trusted service", "best in town"]):
        return "unoptimized_generic_ad", "Ad copy uses generic template cliches without clear conversion hook."
    if "ai" in t and any(w in t for w in ["generated", "prompt", "chatgpt"]):
        return "raw_ai_copy", "Contains unrefined robotic AI ad copy."
    return "static_creative", "Static single-image ad detected. High-conversion opportunity for video/interactive creative upgrade."


def generate_whatsapp_draft(company: str, contact_name: str, offer_cfg: dict, pain_summary: str = "", ad_notes: str = "") -> str:
    """Generate a friendly, concise WhatsApp draft message (safe, never auto-sent)."""
    first_name = contact_name.split()[0] if contact_name else "there"
    sender = offer_cfg.get("sender_name", "Our Team")

    if ad_notes:
        hook = "noticed your ads on Meta"
    elif pain_summary:
        hook = f"came across your work regarding {pain_summary[:40]}"
    else:
        hook = "came across your business"

    return (
        f"Hi {first_name}, {sender} here. I {hook} and had a quick idea on how to increase your booked clients from that. "
        f"Open to a quick 2-minute chat about this?"
    )


def search_meta_api(industry: str, config: dict, city: str = "") -> list[core.Candidate]:
    """Search official Meta Ad Library Graph API if META_ACCESS_TOKEN is provided."""
    token = os.getenv("META_ACCESS_TOKEN") or config.get("discovery", {}).get("meta", {}).get("access_token", "")
    if not token:
        return []

    target = config.get("target", {})
    countries = target.get("countries", ["US"])
    country_code = "US"
    country_map = {"United States": "US", "US": "US", "UK": "GB", "Canada": "CA", "Australia": "AU"}
    if countries:
        country_code = country_map.get(countries[0], countries[0][:2].upper())

    search_query = f"{industry} {city}".strip()
    url = "https://graph.facebook.com/v19.0/ads_archive"
    params = {
        "access_token": token,
        "search_terms": search_query,
        "ad_reached_countries": json.dumps([country_code]),
        "ad_active_status": "ACTIVE",
        "fields": "id,page_id,page_name,ad_creative_bodies,ad_delivery_start_time,ad_snapshot_url,byline",
        "limit": 15,
    }

    candidates: list[core.Candidate] = []
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for item in data:
                    page_name = item.get("page_name", "").strip()
                    bodies = item.get("ad_creative_bodies", [])
                    ad_text = " ".join(bodies)[:500] if bodies else ""
                    snapshot_url = item.get("ad_snapshot_url", "")
                    
                    website = _extract_external_website("", ad_text)
                    if not website:
                        website = resolve_company_website(page_name)

                    ad_qual, ad_notes = audit_ad_quality(ad_text)
                    wa_number = _extract_whatsapp(ad_text)

                    candidates.append(core.Candidate(
                        company=page_name,
                        website=website,
                        signal_name="meta_active_ad",
                        signal_score=60,
                        evidence_url=snapshot_url or f"https://www.facebook.com/ads/library/?active_status=active&q={search_query}",
                        evidence_text=f"Active Meta Ad: {ad_text}" if ad_text else "Active advertiser on Meta platforms",
                        location=city,
                        whatsapp_number=wa_number,
                        ad_quality=ad_qual,
                        ad_audit_notes=ad_notes,
                    ))
    except Exception as exc:
        print(f"[meta-api-notice] {exc}")

    return candidates


def search_meta_web(industry: str, config: dict, city: str = "", max_results: int = 10) -> list[core.Candidate]:
    """Search public Meta Ad Library & Facebook business profiles via search queries."""
    loc_str = f'"{city}"' if city else ""
    queries = [
        f'site:facebook.com/ads/library "{industry}" {loc_str}'.strip(),
        f'site:facebook.com "{industry}" {loc_str} "about" ("website" OR "@" OR "whatsapp")'.strip(),
        f'site:facebook.com "{industry}" {loc_str} "contact us" OR "official website"'.strip(),
    ]

    candidates: list[core.Candidate] = []
    seen: set[str] = set()

    for q in queries:
        try:
            results = core.fetch_ddg_results(q, max_results=max_results)
        except Exception as exc:
            print(f"[meta-search-notice] {exc}")
            continue

        for r in results:
            url = r.get("href") or r.get("url") or ""
            title = r.get("title", "")
            snippet = r.get("snippet") or r.get("body") or ""

            clean_name = re.sub(r"\s*[-|–—:]\s*(Home|Facebook|About|Posts|Photos|Videos|Reviews).*$", "", title, flags=re.I).strip()
            clean_name = re.sub(r"\s*\|\s*Facebook.*$", "", clean_name, flags=re.I).strip()
            if not clean_name or len(clean_name) > 50 or clean_name.lower() in ["facebook", "meta ad library", "log in"]:
                continue

            if clean_name.lower() in seen:
                continue
            seen.add(clean_name.lower())

            website = _extract_external_website(url, snippet)
            if not website:
                website = resolve_company_website(clean_name)

            ad_qual, ad_notes = audit_ad_quality(snippet)
            wa_num = _extract_whatsapp(f"{url} {snippet}")
            
            candidates.append(core.Candidate(
                company=clean_name,
                website=website,
                signal_name="meta_ad_or_page",
                signal_score=50,
                evidence_url=url,
                evidence_text=f"Facebook/Meta signal: {snippet[:400]}",
                location=city,
                whatsapp_number=wa_num,
                ad_quality=ad_qual,
                ad_audit_notes=ad_notes,
            ))

    return candidates


def resolve_company_website(company: str) -> str:
    """Find company's official website if only brand/page name is known."""
    try:
        results = core.fetch_ddg_results(f'"{company}" official site', max_results=3)
        for r in results:
            href = r.get("href") or r.get("url") or ""
            if not href:
                continue
            p = urlparse(href)
            domain = p.netloc.lower().removeprefix("www.")
            if domain and domain not in core.SKIP_HOSTS and not domain.endswith("facebook.com") and "." in domain:
                return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return ""


def import_csv_leads(csv_path: str | Path) -> list[core.Candidate]:
    """Import pre-existing list of leads/websites from a CSV file."""
    path = Path(csv_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        print(f"[csv-import] File not found: {path}")
        return []

    candidates: list[core.Candidate] = []
    try:
        with open(path, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                company = row.get("company") or row.get("Company") or row.get("name") or row.get("Name") or ""
                website = row.get("website") or row.get("Website") or row.get("url") or row.get("URL") or ""
                email = row.get("email") or row.get("Email") or ""
                whatsapp = row.get("whatsapp") or row.get("WhatsApp") or row.get("phone") or ""
                location = row.get("location") or row.get("city") or ""

                if not website and not company:
                    continue

                if not company and website:
                    company = core.company_from_result("", website)

                candidates.append(core.Candidate(
                    company=company.strip(),
                    website=core.normalize_url(website.strip()) if website else "",
                    signal_name="imported_csv_lead",
                    signal_score=50,
                    evidence_url=website.strip(),
                    evidence_text=f"Imported from user CSV list: {company}",
                    email=email.strip() if email else None,
                    whatsapp_number=whatsapp.strip(),
                    location=location.strip(),
                ))
        print(f"[csv-import] Loaded {len(candidates)} lead(s) from {path.name}")
    except Exception as exc:
        print(f"[csv-import-error] Failed to parse {path}: {exc}")

    return candidates


def search_meta_candidates(config: dict) -> list[core.Candidate]:
    """Search both Meta API and public Meta / Facebook sources across target industries and cities."""
    target = config.get("target", {})
    industries = target.get("industries", [])
    cities = target.get("cities", [""])
    if not cities:
        cities = [""]
    filters = config.get("filters", {})
    excluded = {i.lower() for i in filters.get("excluded_industries", [])}

    all_candidates: list[core.Candidate] = []
    for ind in industries:
        if stop_controller.is_stop_requested(config):
            break
        if ind.lower() in excluded:
            continue
        for city in cities:
            if stop_controller.is_stop_requested(config):
                break
            loc_label = f" in {city}" if city else ""
            print(f"[meta-search] Scanning Meta Ads & Facebook pages for: {ind}{loc_label}")
            # Official API
            api_results = search_meta_api(ind, config, city=city)
            if api_results:
                all_candidates.extend(api_results)
            # Web search companion
            web_results = search_meta_web(ind, config, city=city)
            all_candidates.extend(web_results)

    return all_candidates
