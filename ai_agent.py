from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Type

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent


class QualificationResult(BaseModel):
    qualified: bool
    score: int = Field(ge=0, le=100)
    confidence: int = Field(ge=0, le=100)
    pain_summary: str
    reason: str
    evidence_quote: str


class EmailResult(BaseModel):
    subject: str
    body: str


class CampaignSettingsResult(BaseModel):
    offer_name: str
    offer_description: str
    sender_name: str
    cta: str
    target_industries: list[str]
    target_countries: list[str]
    target_cities: list[str]
    blacklist_keywords: list[str]


@dataclass
class AgentResult:
    qualified: bool
    score: int
    confidence: int
    pain_summary: str
    reason: str
    evidence_quote: str


PROVIDERS = {
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "nvidia": {
        "key_env": "NVIDIA_API_KEY",
        "model_env": "NVIDIA_MODEL",
        "default_model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "base_url": "https://integrate.api.nvidia.com/v1",
    },
    "openrouter": {
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "openrouter/free",
        "base_url": "https://openrouter.ai/api/v1",
    },
}

DEFAULT_PROVIDER_ORDER = ["gemini", "nvidia", "openrouter"]

# Task-specific Gemini models for maximum speed, accuracy, and burnout prevention
GEMINI_TASK_MODELS = {
    "qualification": os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    "email": os.getenv("GEMINI_PRO_MODEL", "gemini-2.5-pro"),
    "ad_audit": os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
    "autofill": os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
}

# Multi-key rotation state
_gemini_key_index = 0
_gemini_key_lock = threading.Lock()


def _load_env() -> None:
    load_dotenv(ROOT / ".env")


def get_gemini_api_keys() -> list[str]:
    """Retrieves all available Gemini keys from environment variables."""
    _load_env()
    keys: list[str] = []

    # 1. Primary key (supports comma-separated list)
    primary = os.getenv("GEMINI_API_KEY", "").strip()
    if primary:
        for k in primary.split(","):
            clean = k.strip()
            if clean and clean not in keys:
                keys.append(clean)

    # 2. Numbered keys: GEMINI_API_KEY_2, GEMINI_API_KEY_3, etc.
    for i in range(2, 11):
        val = os.getenv(f"GEMINI_API_KEY_{i}", "").strip()
        if val and val not in keys:
            keys.append(val)

    # 3. Dedicated plural env
    plural = os.getenv("GEMINI_API_KEYS", "").strip()
    if plural:
        for k in plural.split(","):
            clean = k.strip()
            if clean and clean not in keys:
                keys.append(clean)

    return keys


def get_active_gemini_key(rotate: bool = False) -> str:
    """Returns an active Gemini API key, rotating to the next key if requested or on rate limit."""
    global _gemini_key_index
    keys = get_gemini_api_keys()
    if not keys:
        return ""
    with _gemini_key_lock:
        if rotate:
            _gemini_key_index = (_gemini_key_index + 1) % len(keys)
        idx = _gemini_key_index % len(keys)
        return keys[idx]


def _provider_order(config: dict) -> list[str]:
    configured = config.get("ai", {}).get("provider_order", DEFAULT_PROVIDER_ORDER)
    if isinstance(configured, str):
        configured = [p.strip() for p in configured.split(",") if p.strip()]
    return [p.lower() for p in configured if p.lower() in PROVIDERS]


def _provider_ready(name: str) -> bool:
    if name == "gemini":
        return len(get_gemini_api_keys()) > 0
    spec = PROVIDERS[name]
    return bool(os.getenv(spec["key_env"], "").strip())


def available(config: dict) -> bool:
    if not config.get("ai", {}).get("enabled", True):
        return False
    _load_env()
    return any(_provider_ready(name) for name in _provider_order(config))


def _client_for(name: str, task: str = "qualification", rotate_key: bool = False) -> tuple[OpenAI, str]:
    spec = PROVIDERS[name]
    if name == "gemini":
        api_key = get_active_gemini_key(rotate=rotate_key)
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        # Task specialized model selection
        model = os.getenv(spec["model_env"], "").strip() or GEMINI_TASK_MODELS.get(task, "gemini-2.5-flash")
    else:
        api_key = os.getenv(spec["key_env"], "").strip()
        if not api_key:
            raise RuntimeError(f"{spec['key_env']} is not configured")
        model = os.getenv(spec["model_env"], "").strip() or spec["default_model"]

    kwargs = {
        "api_key": api_key,
        "base_url": spec["base_url"],
        "timeout": 40.0,
        "max_retries": 1,
    }

    if name == "openrouter":
        headers = {"X-Title": os.getenv("OPENROUTER_APP_NAME", "Outreach Engine")}
        app_url = os.getenv("OPENROUTER_APP_URL", "").strip()
        if app_url:
            headers["HTTP-Referer"] = app_url
        kwargs["default_headers"] = headers

    return OpenAI(**kwargs), model


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        value = json.loads(text, strict=False)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1], strict=False)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    raise ValueError("Model did not return a valid JSON object")


def call_llm_json(*, prompt: str, config: dict, temperature: float,
                  schema: Type[BaseModel], max_tokens: int = 900,
                  task: str = "qualification") -> tuple[BaseModel, str] | None:
    """
    Tries providers with task-specialized models, structured JSON validation,
    and automatic API key rotation on rate-limits (HTTP 429).
    """
    if not config.get("ai", {}).get("enabled", True):
        return None

    _load_env()
    attempted = False

    for provider in _provider_order(config):
        if not _provider_ready(provider):
            continue
        attempted = True

        # Attempt up to 2 keys if Gemini encounters rate-limits
        max_provider_attempts = 2 if provider == "gemini" and len(get_gemini_api_keys()) > 1 else 1
        for attempt in range(max_provider_attempts):
            rotate_now = (attempt > 0)
            try:
                client, model = _client_for(provider, task=task, rotate_key=rotate_now)
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": "Return only valid JSON. Do not use markdown fences or add commentary outside the JSON object.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content = response.choices[0].message.content or ""
                data = _extract_json(content)
                parsed = schema.model_validate(data)
                return parsed, provider
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                print(f"[llm-fallback] {provider} ({model}) returned invalid structured output: {exc}")
                break
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower():
                    print(f"[llm-ratelimit] {provider} key hit rate limit. Rotating API key and backing off...")
                    get_active_gemini_key(rotate=True)
                    # If this was a Pro model in copywriting, fallback to Flash immediately
                    if task == "email" and "pro" in model:
                        try:
                            flash_model = GEMINI_TASK_MODELS.get("qualification", "gemini-2.5-flash")
                            fallback_client, _ = _client_for("gemini", task="qualification", rotate_key=False)
                            res = fallback_client.chat.completions.create(
                                model=flash_model,
                                messages=[
                                    {"role": "system", "content": "Return only valid JSON."},
                                    {"role": "user", "content": prompt},
                                ],
                                temperature=temperature,
                                max_tokens=max_tokens,
                            )
                            parsed = schema.model_validate(_extract_json(res.choices[0].message.content or ""))
                            return parsed, f"{provider}-flash-fallback"
                        except Exception:
                            pass
                    continue
                else:
                    print(f"[llm-fallback] {provider} ({model}) failed: {exc}")
                    break

    if not attempted:
        print("[llm] No provider API key configured.")
    return None


def get_feedback_context() -> str:
    """Retrieve top approved and rejected leads to steer the AI based on founder ratings."""
    try:
        import sqlite3
        conn = sqlite3.connect(ROOT / "leads.db", timeout=5.0)
        conn.row_factory = sqlite3.Row
        liked = conn.execute("SELECT company, signal_name, pain_summary, feedback_note FROM leads WHERE rating > 0 ORDER BY updated_at DESC LIMIT 3").fetchall()
        disliked = conn.execute("SELECT company, signal_name, pain_summary, feedback_note FROM leads WHERE rating < 0 ORDER BY updated_at DESC LIMIT 3").fetchall()
        conn.close()

        ctx_parts = []
        if liked:
            ctx_parts.append("PREVIOUS HIGH-RATED LEADS BY FOUNDER (MODEL PREFERENCE):")
            for r in liked:
                note = f" (Founder feedback: {r['feedback_note']})" if r['feedback_note'] else ""
                ctx_parts.append(f"- {r['company']}: {r['pain_summary']}{note}")
        if disliked:
            ctx_parts.append("PREVIOUS REJECTED LEADS BY FOUNDER (AVOID SIMILAR):")
            for r in disliked:
                note = f" (Founder feedback: {r['feedback_note']})" if r['feedback_note'] else ""
                ctx_parts.append(f"- {r['company']}: {r['pain_summary']}{note}")
        return "\n".join(ctx_parts)
    except Exception:
        return ""


def qualify_lead(*, company: str, website: str, signal_name: str, signal_score: int,
                 evidence_url: str, evidence_text: str, research_text: str,
                 config: dict) -> AgentResult | None:
    offer = config.get("offer", {})
    target = config.get("target", {})
    feedback_section = get_feedback_context()
    feedback_text = f"\nFOUNDER FEEDBACK & HISTORICAL PREFERENCES:\n{feedback_section}\n" if feedback_section else ""

    prompt = f"""
You are a strict B2B prospect qualification analyst specializing in AI Commercial Video Production.

Goal: decide whether this business has OBSERVABLE evidence of a problem that AI Commercial Video Production can plausibly solve (e.g. running static image ads, ad creative fatigue, high customer acquisition cost, unengaging product videos, lack of high-converting social video ads).
Do not infer pain merely because the company belongs to a target industry.
Do not invent facts, people, revenue, or operational problems.
If evidence is weak or ambiguous, set qualified=false.
{feedback_text}
OFFER
Name: {offer.get('name', 'AI Commercial Video Production')}
Description: {offer.get('description', '')}
Target industries: {', '.join(target.get('industries', []))}
Target countries: {', '.join(target.get('countries', []))}

PROSPECT
Company: {company}
Website: {website}
Detected signal: {signal_name}
Heuristic signal score: {signal_score}/100
Evidence URL: {evidence_url}
Search evidence: {evidence_text[:3000]}
Website research: {research_text[:14000]}

Scoring rubric:
0-29: no useful evidence / likely irrelevant
30-49: weak but plausible signal
50-69: clear problem signal and reasonable offer fit (e.g. brand running static ads or selling visually rich products)
70-84: strong recent need signal (active static ad spenders, new product launches)
85-100: unusually explicit buying need signal for video creatives

Return exactly this JSON shape:
{{
  "qualified": true,
  "score": 0,
  "confidence": 0,
  "pain_summary": "one factual sentence focusing on ad creative or commercial video needs",
  "reason": "concise evidence-based explanation",
  "evidence_quote": "shortest useful exact fragment or empty string"
}}
"""

    raw = call_llm_json(
        prompt=prompt,
        config=config,
        temperature=0.1,
        max_tokens=2000,
        schema=QualificationResult,
        task="qualification",
    )
    if not raw:
        return None

    parsed, _provider = raw
    return AgentResult(
        qualified=parsed.qualified,
        score=parsed.score,
        confidence=parsed.confidence,
        pain_summary=parsed.pain_summary.strip(),
        reason=parsed.reason.strip(),
        evidence_quote=parsed.evidence_quote.strip(),
    )


def write_email(*, company: str, signal_name: str, evidence_text: str,
                pain_summary: str, ai_reason: str, contact_name: str = "",
                contact_title: str = "", config: dict) -> tuple[str, str] | None:
    offer = config.get("offer", {})
    max_words = int(config.get("ai", {}).get("max_email_words", 75))
    sender_name = offer.get("sender_name", "").strip() or "Saad"
    first_name = contact_name.split()[0].strip() if contact_name.strip() else ""
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    service_name = offer.get("name", "AI Commercial Video Production").strip() or "AI Commercial Video Production"
    service_desc = offer.get("description", "").strip() or "creating high-converting commercial video ads that replace fatigued static creatives"
    cta = offer.get("cta", "Would you be open to seeing a free 30-second AI video storyboard concepts for your best-selling product this week?").strip()

    prompt = f"""
Write a short, highly personalized, casual cold outreach email offering {service_name}.

CRITICAL REQUIREMENTS:
- ABSOLUTELY NO PLACEHOLDERS: NEVER use brackets like [Your Name], [Company], [Product], [Insert Link], or [Name]. The email must be 100% finished and ready to send immediately.
- CONTRACT / B2B SERVICE ONLY: You are offering {service_name} ({service_desc}). Never ask for employment or a salary.
- LENGTH & STYLE: Extremely concise (3 to 5 sentences max, under {max_words} words). Very natural, direct, and conversational human tone. No corporate fluff, fake compliments, or robotic buzzwords.
- SUBJECT LINE: Short, natural 2-5 words (e.g. "quick question for {company}" or "video creatives for {company}").
- GREETING: Must start exactly with "{greeting}".
- CALL TO ACTION: Use this exact CTA: "{cta}".
- SIGN OFF: Sign off with "Best,\n{sender_name}".

PROSPECT
Company: {company}
Decision Maker: {contact_name or 'Team'}
Title: {contact_title or ''}
Signal: {signal_name}
Evidence: {evidence_text[:3000]}
Context: {pain_summary}

OFFER
Service: {service_name}
Description: {service_desc}
Sender: {sender_name}

Return strictly this JSON:
{{
  "subject": "natural short subject",
  "body": "100% complete finished plain text email without any brackets or placeholders"
}}
"""

    raw = call_llm_json(
        prompt=prompt,
        config=config,
        temperature=0.3,
        max_tokens=2000,
        schema=EmailResult,
        task="email",
    )
    if not raw:
        return None

    parsed, _provider = raw
    subject = parsed.subject.strip().replace("\n", " ")[:120]
    body = parsed.body.strip()
    if not subject or not body:
        return None

    # Post-process cleanup: strip any accidental bracketed placeholders
    body = re.sub(r"\[.*?\]", "", body).strip()
    body = re.sub(r"<.*?>", "", body).strip()

    if sender_name.lower() not in body.lower():
        body = body.rstrip() + f"\n\nBest,\n{sender_name}"

    return subject, body


def infer_campaign_settings(current_config: dict) -> dict:
    """
    If any campaign settings or targeting fields are empty, AI automatically decides
    the highest-yield parameters for AI Commercial Video Production targeting mid-sized commercial brands.
    """
    # High-quality strategic defaults (used if offline or as baseline)
    defaults = {
        "offer_name": "AI Commercial Video Production",
        "offer_description": "Producing high-converting 15-30s AI commercial video ads that replace exhausted static image ads to lower customer acquisition costs and boost ad spend returns.",
        "sender_name": "Saad",
        "cta": "Would you be open to seeing a free 30-second AI video storyboard concepts for your best-selling product this week?",
        "target_industries": [
            "Skincare & Beauty",
            "Apparel & Athleisure",
            "Functional Beverages & Nutrition",
            "Supplements & Wellness",
            "Pet Care Essentials",
            "Home Goods & Innovative Gadgets"
        ],
        "target_countries": ["United States", "United Kingdom", "Canada", "Australia"],
        "target_cities": ["Miami", "Los Angeles", "New York", "Austin", "London", "Toronto"],
        "daily_cap": 25,
        "send_mode": "draft",
        "blacklist_keywords": [
            "Nike", "Adidas", "Apple", "Amazon", "Walmart", "Target", "eBay",
            "job", "intern", "hiring", "charity", "free", "non-profit", "cheap", "recruitment"
        ]
    }

    if not available(current_config):
        return defaults

    prompt = f"""
You are an expert Chief Marketing Officer and B2B growth strategist specializing in AI Commercial Video Production for mid-sized direct-to-consumer and commercial brands.

A founder needs optimal targeting parameters. Analyze the market for AI Commercial Video (brands spending money on static ads who need dynamic high-converting video ads).

Fill out optimal values:
- offer_name: clear commercial video offer title
- offer_description: 1-2 sentence compelling value proposition (fatigued ads, video conversion, lower CPA)
- sender_name: professional sender name
- cta: soft, low-friction call-to-action (e.g. free sample storyboard / sample video concept)
- target_industries: list of 5-8 mid-market industries with visually appealing products
- target_countries: top 3-4 e-commerce buying countries
- target_cities: 4-6 major DTC/commercial brand hubs
- blacklist_keywords: list of negative keywords to avoid mega-corps (Nike, Apple) or non-buyers (charity, job, intern)

Return strictly this JSON:
{{
  "offer_name": "AI Commercial Video Production",
  "offer_description": "...",
  "sender_name": "Saad",
  "cta": "...",
  "target_industries": ["..."],
  "target_countries": ["..."],
  "target_cities": ["..."],
  "blacklist_keywords": ["..."]
}}
"""

    raw = call_llm_json(
        prompt=prompt,
        config=current_config,
        temperature=0.2,
        max_tokens=2000,
        schema=CampaignSettingsResult,
        task="autofill",
    )

    if not raw:
        return defaults

    parsed, _ = raw
    return {
        "offer_name": parsed.offer_name or defaults["offer_name"],
        "offer_description": parsed.offer_description or defaults["offer_description"],
        "sender_name": parsed.sender_name or defaults["sender_name"],
        "cta": parsed.cta or defaults["cta"],
        "target_industries": parsed.target_industries or defaults["target_industries"],
        "target_countries": parsed.target_countries or defaults["target_countries"],
        "target_cities": parsed.target_cities or defaults["target_cities"],
        "daily_cap": 25,
        "send_mode": "draft",
        "blacklist_keywords": parsed.blacklist_keywords or defaults["blacklist_keywords"],
    }
