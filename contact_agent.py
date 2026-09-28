from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import dns.resolver
from ddgs import DDGS

import main as core


ROLE_PRIORITY = {
    "founder": 100,
    "co-founder": 99,
    "owner": 98,
    "ceo": 96,
    "chief marketing officer": 95,
    "cmo": 94,
    "head of marketing": 92,
    "creative director": 90,
    "marketing director": 88,
    "brand director": 86,
    "president": 84,
    "managing partner": 80,
    "general manager": 75,
    "director": 70,
    "manager": 55,
}

ROLE_EMAIL_PREFIXES = [
    "owner", "founder", "ceo", "president", "director", "manager",
    "office", "operations", "sales", "hello", "contact", "info",
]

NAME_ROLE_PATTERNS = [
    re.compile(
        rf"(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z'.-]+){{1,3}})\s*[,|\-–—:]\s*(?P<role>{'|'.join(re.escape(r) for r in ROLE_PRIORITY)})\b",
        re.I,
    ),
    re.compile(
        rf"(?P<role>{'|'.join(re.escape(r) for r in ROLE_PRIORITY)})\s*[:|\-–—]?\s*(?P<name>[A-Z][a-z]+(?:\s+[A-Z][a-z'.-]+){{1,3}})",
        re.I,
    ),
]


@dataclass
class ContactCandidate:
    name: str = ""
    title: str = ""
    email: str = ""
    source_url: str = ""
    source_text: str = ""
    confidence: int = 0
    mx_valid: bool = False
    direct_match: bool = False


def _root_domain(website: str) -> str:
    return urlparse(website).netloc.lower().removeprefix("www.")


def _normalize_role(role: str) -> str:
    return re.sub(r"\s+", " ", (role or "").strip().lower())


def _role_score(role: str) -> int:
    role = _normalize_role(role)
    for key, score in ROLE_PRIORITY.items():
        if key in role:
            return score
    return 0


def _name_tokens(name: str) -> list[str]:
    return [re.sub(r"[^a-z]", "", p.lower()) for p in (name or "").split() if p]


def _email_matches_name(email: str, name: str) -> bool:
    local = email.split("@", 1)[0].lower()
    tokens = [t for t in _name_tokens(name) if len(t) >= 3]
    if not tokens:
        return False
    return any(t in local for t in tokens)


def _mx_valid(email: str) -> bool:
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip()
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=4)
        return bool(list(answers))
    except Exception:
        return False


def _extract_name_roles(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for pattern in NAME_ROLE_PATTERNS:
        for match in pattern.finditer(text or ""):
            name = re.sub(r"\s+", " ", match.group("name")).strip(" ,|:-")
            role = _normalize_role(match.group("role"))
            if len(name.split()) < 2 or len(name) > 80:
                continue
            pair = (name, role)
            if pair not in found:
                found.append(pair)
    return found


def _public_emails(text: str, website: str) -> list[str]:
    return core.extract_public_emails(text or "", website)


def _search_public_evidence(company: str, website: str, max_results: int = 8) -> list[dict]:
    domain = _root_domain(website)
    queries = [
        f'"{company}" founder',
        f'"{company}" CEO',
        f'"{company}" "head of marketing"',
        f'"{company}" "creative director"',
        f'"{company}" CMO',
        f'"{company}" owner',
        f'site:{domain} founder OR CEO OR marketing',
        f'site:{domain} contact OR team',
    ]
    results: list[dict] = []
    seen: set[str] = set()
    with DDGS() as ddgs:
        for query in queries:
            try:
                rows = ddgs.text(query, max_results=max_results)
            except Exception as exc:
                print(f"[contact-search-error] {company}: {exc}")
                continue
            for row in rows:
                url = row.get("href") or row.get("url") or ""
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({
                    "url": url,
                    "title": row.get("title") or "",
                    "text": row.get("body") or row.get("snippet") or "",
                })
    return results


def _acceptable_email_domain(email: str, website: str) -> bool:
    domain = email.rsplit("@", 1)[1].lower()
    root = _root_domain(website)
    return domain == root or domain.endswith("." + root) or domain in core.COMMON_FREE_EMAILS


def _score_email(email: str, name: str, title: str, website: str, mx_ok: bool) -> tuple[int, bool]:
    domain = email.rsplit("@", 1)[1].lower()
    root = _root_domain(website)
    local = email.split("@", 1)[0].lower()
    score = 0
    direct_match = _email_matches_name(email, name)

    if domain == root or domain.endswith("." + root):
        score += 40
    elif domain in core.COMMON_FREE_EMAILS:
        score += 8
    if mx_ok:
        score += 15
    if direct_match:
        score += 28
    if any(local == p or local.startswith(p + ".") or local.startswith(p + "-") for p in ROLE_EMAIL_PREFIXES):
        score += 10
    score += min(7, _role_score(title) // 14)
    return min(100, score), direct_match


def find_best_contact(*, company: str, website: str, research_text: str,
                      existing_email: str | None = None, config: dict | None = None) -> ContactCandidate | None:
    config = config or {}
    contact_cfg = config.get("contact_intelligence", {})
    if not contact_cfg.get("enabled", True):
        if existing_email:
            mx_ok = _mx_valid(existing_email)
            return ContactCandidate(email=existing_email, confidence=50 if mx_ok else 35, mx_valid=mx_ok)
        return None

    max_results = int(contact_cfg.get("max_search_results", 6))
    min_confidence = int(contact_cfg.get("min_confidence", 45))

    evidence = _search_public_evidence(company, website, max_results=max_results)
    combined_sources: list[tuple[str, str]] = [(website, research_text or "")]
    for row in evidence:
        combined_sources.append((row["url"], f"{row['title']}\n{row['text']}"))

    people: list[tuple[str, str, str, str]] = []
    emails: set[str] = set()
    if existing_email and _acceptable_email_domain(existing_email, website):
        emails.add(existing_email.lower())

    for source_url, text in combined_sources:
        for email in _public_emails(text, website):
            if _acceptable_email_domain(email, website):
                emails.add(email.lower())
        for name, role in _extract_name_roles(text):
            people.append((name, role, source_url, text[:1000]))

    if not emails:
        return None

    if not people:
        best_email = ""
        best_score = -1
        best_mx = False
        for email in sorted(emails):
            mx_ok = _mx_valid(email)
            score, _ = _score_email(email, "", "", website, mx_ok)
            if score > best_score:
                best_email, best_score, best_mx = email, score, mx_ok
        if best_score < min_confidence:
            return None
        return ContactCandidate(
            email=best_email,
            source_url=website,
            source_text="Public business email; named decision-maker not confidently linked to this inbox.",
            confidence=best_score,
            mx_valid=best_mx,
            direct_match=False,
        )

    best: ContactCandidate | None = None
    for name, title, source_url, source_text in people:
        for email in emails:
            mx_ok = _mx_valid(email)
            score, direct_match = _score_email(email, name, title, website, mx_ok)
            candidate = ContactCandidate(
                name=name,
                title=title,
                email=email,
                source_url=source_url,
                source_text=source_text,
                confidence=score,
                mx_valid=mx_ok,
                direct_match=direct_match,
            )
            if best is None or candidate.confidence > best.confidence:
                best = candidate

    if best and best.confidence >= min_confidence:
        return best
    return None
