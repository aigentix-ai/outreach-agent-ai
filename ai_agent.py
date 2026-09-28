from __future__ import annotations

import json
import os
import re
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


@dataclass
class AgentResult:
    qualified: bool
    score: int
    confidence: int
    pain_summary: str
    reason: str
    evidence_quote: str


PROVIDERS = {
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
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-3.8-flash",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
}

DEFAULT_PROVIDER_ORDER = ["nvidia", "openrouter", "gemini"]


def _load_env() -> None:
    load_dotenv(ROOT / ".env")


def _provider_order(config: dict) -> list[str]:
    configured = config.get("ai", {}).get("provider_order", DEFAULT_PROVIDER_ORDER)
    if isinstance(configured, str):
        configured = [p.strip() for p in configured.split(",") if p.strip()]
    return [p.lower() for p in configured if p.lower() in PROVIDERS]


def _provider_ready(name: str) -> bool:
    spec = PROVIDERS[name]
    return bool(os.getenv(spec["key_env"], "").strip())


def available(config: dict) -> bool:
    if not config.get("ai", {}).get("enabled", True):
        return False
    _load_env()
    return any(_provider_ready(name) for name in _provider_order(config))


def _client_for(name: str) -> tuple[OpenAI, str]:
    spec = PROVIDERS[name]
    api_key = os.getenv(spec["key_env"], "").strip()
    if not api_key:
        raise RuntimeError(f"{spec['key_env']} is not configured")

    model = os.getenv(spec["model_env"], "").strip() or spec["default_model"]
    kwargs = {
        "api_key": api_key,
        "base_url": spec["base_url"],
        "timeout": 45.0,
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
                  schema: Type[BaseModel], max_tokens: int = 900) -> tuple[BaseModel, str] | None:
    """Try providers in order. A provider only succeeds after JSON + schema validation."""
    if not config.get("ai", {}).get("enabled", True):
        return None

    _load_env()
    attempted = False

    for provider in _provider_order(config):
        if not _provider_ready(provider):
            continue
        attempted = True
        try:
            client, model = _client_for(provider)
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
            print(f"[llm-fallback] {provider} returned invalid structured output: {exc}. Raw content: {repr(content)}")
        except Exception as exc:
            print(f"[llm-fallback] {provider} failed: {exc}")

    if not attempted:
        print("[llm] No provider API key configured.")
    return None


def qualify_lead(*, company: str, website: str, signal_name: str, signal_score: int,
                 evidence_url: str, evidence_text: str, research_text: str,
                 config: dict) -> AgentResult | None:
    offer = config.get("offer", {})
    target = config.get("target", {})
    prompt = f"""
You are a strict B2B prospect qualification analyst.

Goal: decide whether this business has OBSERVABLE evidence of a problem that the offer can plausibly solve.
Do not infer a pain merely because the company belongs to a target industry.
Do not invent facts, people, revenue, tools, call volume, or operational problems.
If evidence is weak or ambiguous, set qualified=false.

OFFER
Name: {offer.get('name', '')}
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
50-69: clear problem signal and reasonable offer fit
70-84: strong recent/direct need signal
85-100: unusually explicit buying/need signal

Return exactly this JSON shape:
{{
  "qualified": true,
  "score": 0,
  "confidence": 0,
  "pain_summary": "one factual sentence",
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

    prompt = f"""
Write a short, highly personalized, casual cold outreach email for a freelance / contract AI video commercial project.

CRITICAL REQUIREMENTS:
- ABSOLUTELY NO PLACEHOLDERS: NEVER use brackets like [Your Name], [Company], [Product], [Insert Link], or [Name]. The email must be 100% finished and ready to send immediately.
- CONTRACT / FREELANCE ONLY: You are a specialist offering freelance / project-based commercial video production. Never ask for a job, full-time employment, benefits, or a salary.
- LENGTH & STYLE: Extremely concise (3 to 5 sentences max, under {max_words} words). Very natural, direct, and conversational human tone. No corporate fluff, fake compliments, or robotic buzzwords.
- SUBJECT LINE: Short, natural 2-5 words (e.g. "quick video idea for {company}" or "{company}'s video ads").
- GREETING: Must start exactly with "{greeting}".
- CALL TO ACTION: Use this exact CTA: "{offer.get('cta', 'Would you be open to seeing a free 10-second sample video commercial tailored for your brand?')}".
- SIGN OFF: Sign off with "Best,\n{sender_name}".

PROSPECT
Company: {company}
Decision Maker: {contact_name or 'Team'}
Title: {contact_title or ''}
Signal: {signal_name}
Evidence: {evidence_text[:3000]}
Context: {pain_summary}

OFFER
Service: {offer.get('name', 'AI Video Commercials')}
Description: {offer.get('description', '')}
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
