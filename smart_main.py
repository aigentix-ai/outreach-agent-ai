from __future__ import annotations

import argparse
import sqlite3
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import main as core
import ai_agent
from ai_agent import available as ai_available
from ai_agent import qualify_lead, write_email
from contact_agent import find_best_contact
from reporter import RunReport
import meta_agent
import stop_controller


EXTRA_COLUMNS = {
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
}


def ensure_extra_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    for name, sql_type in EXTRA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {sql_type}")
    conn.commit()


def smart_research(candidate: core.Candidate, config: dict) -> tuple[core.Candidate, str]:
    crawler = config.get("crawler", {})
    timeout = int(crawler.get("timeout_seconds", 12))
    max_pages = int(crawler.get("max_pages_per_domain", 7))
    ua = crawler.get("user_agent", "Mozilla/5.0")
    pages = ["/", "/contact", "/contact-us", "/about", "/team", "/leadership", "/careers", "/jobs"][:max_pages]
    texts: list[str] = []
    emails: list[str] = []

    with httpx.Client(timeout=timeout, headers={"User-Agent": ua}) as client:
        for path in pages:
            if stop_controller.is_stop_requested(config):
                break
            url = urljoin(candidate.website, path)
            html = core.fetch_html(client, url)
            if not html:
                continue
            emails.extend(core.extract_public_emails(html, candidate.website))
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "noscript", "svg"]):
                tag.decompose()
            text = " ".join(soup.stripped_strings)
            if text:
                texts.append(f"PAGE {url}\n{text[:10000]}")

    if not candidate.email and emails:
        candidate.email = emails[0]
    research_text = "\n\n".join(texts)[:30000]
    corpus = research_text.lower()
    terms = [w.lower() for w in candidate.signal_name.replace("_", " ").split() if len(w) > 3]
    offer_words = [w.lower() for w in f"{config.get('offer', {}).get('name', '')} {config.get('offer', {}).get('description', '')}".split() if len(w) > 3]
    terms.extend(offer_words)
    terms.extend(["contact", "owner", "service", "pricing", "quote", "booking", "schedule", "reception"])
    bonus = sum(5 for term in set(terms) if term in corpus)
    candidate.score = min(100, candidate.signal_score + min(bonus, 30))
    return candidate, research_text


def save_enrichment(conn: sqlite3.Connection, candidate: core.Candidate, research_text: str,
                    ai_result=None, contact=None, config: dict | None = None) -> None:
    fp = core.fingerprint(candidate.company, candidate.website, candidate.email)
    fields = {
        "research_text": research_text,
        "updated_at": core.now_iso(),
    }
    if candidate.location:
        fields["location"] = candidate.location
    if candidate.whatsapp_number:
        fields["whatsapp_number"] = candidate.whatsapp_number
    if candidate.ad_quality:
        fields["ad_quality"] = candidate.ad_quality
    if candidate.ad_audit_notes:
        fields["ad_audit_notes"] = candidate.ad_audit_notes

    # Generate WhatsApp message draft if whatsapp number exists
    if candidate.whatsapp_number and config:
        contact_name = contact.name if contact else ""
        pain_summary = ai_result.pain_summary if ai_result else ""
        ad_notes = candidate.ad_audit_notes or ""
        fields["whatsapp_draft"] = meta_agent.generate_whatsapp_draft(
            company=candidate.company,
            contact_name=contact_name,
            offer_cfg=config.get("offer", {}),
            pain_summary=pain_summary,
            ad_notes=ad_notes,
        )

    if ai_result:
        fields.update({
            "pain_summary": ai_result.pain_summary,
            "ai_reason": ai_result.reason,
            "ai_confidence": ai_result.confidence,
            "ai_evidence_quote": ai_result.evidence_quote,
            "score": ai_result.score,
        })
    if contact:
        fields.update({
            "contact_name": contact.name,
            "contact_title": contact.title,
            "contact_source_url": contact.source_url,
            "contact_source_text": contact.source_text,
            "contact_confidence": contact.confidence,
            "contact_direct_match": 1 if contact.direct_match else 0,
            "email_mx_valid": 1 if contact.mx_valid else 0,
        })

    assignments = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values()) + [fp]
    conn.execute(f"UPDATE leads SET {assignments} WHERE fingerprint=?", values)
    conn.commit()


def discover(conn: sqlite3.Connection, config: dict, report: RunReport | None = None) -> RunReport:
    ensure_extra_columns(conn)
    should_finish = False
    if report is None:
        report = RunReport(mode="discover")
        should_finish = True

    # Check Kill Switch before starting
    if stop_controller.is_stop_requested(config):
        print("\n[EMERGENCY STOP] Kill switch is active. Halting discovery before start.")
        if should_finish:
            report.finish()
        return report

    # If key targeting or offer fields are empty, let AI decide the best parameters automatically
    if not config.get("target", {}).get("industries") or not config.get("offer", {}).get("name"):
        inferred = ai_agent.infer_campaign_settings(config)
        config.setdefault("target", {})["industries"] = inferred["target_industries"]
        config.setdefault("target", {})["countries"] = inferred["target_countries"]
        config.setdefault("target", {})["cities"] = inferred["target_cities"]
        config.setdefault("offer", {})["name"] = inferred["offer_name"]
        config.setdefault("offer", {})["description"] = inferred["offer_description"]
        config.setdefault("offer", {})["cta"] = inferred["cta"]
        print(f"[ai-autofill] Empty settings detected. AI auto-applied commercial video parameters: {inferred['target_industries']}")

    candidates = core.search_candidates(config, reporter=report)
    print(f"Search produced {len(candidates)} candidate result(s). Researching + contact-enriching + qualifying...")
    if report:
        report.save()

    qualification = config.get("qualification", {})
    ai_cfg = config.get("ai", {})
    min_score = int(qualification.get("min_score", 50))
    require_email = bool(qualification.get("require_public_email", True))
    min_heuristic = int(ai_cfg.get("min_heuristic_score_before_ai", 15))
    min_confidence = int(ai_cfg.get("min_confidence", 55))
    use_ai = ai_available(config)
    created = 0

    if not use_ai:
        print("[ai] No configured LLM provider key found; using heuristic qualification fallback.")

    for i, candidate in enumerate(candidates, 1):
        if stop_controller.is_stop_requested(config):
            print("\n[EMERGENCY STOP] Kill switch detected! Halting discovery loop immediately.")
            report.notes.append("Discovery halted early by User Kill Switch.")
            break

        candidate, research_text = smart_research(candidate, config)

        # Dynamic Blacklist Filter (Company, URL, or Website Research Text)
        is_blocked, block_reason = core.check_blacklist(candidate.company, candidate.website, research_text, config)
        if is_blocked:
            print(f"[blacklist-reject] {candidate.company}: {block_reason}")
            report.log_blacklist(candidate.company, block_reason)
            continue

        # Optional E-commerce / DTC Store Filter (only runs if configured or targeting retail/e-commerce)
        require_ecommerce = config.get("filters", {}).get("require_ecommerce_store", False) or any(
            ind.lower() in ["ecommerce", "e-commerce", "d2c", "dtc", "retail", "store"]
            for ind in config.get("target", {}).get("industries", [])
        )
        if require_ecommerce:
            corpus = research_text.lower()
            ecommerce_signals = ["add to cart", "cart", "checkout", "shipping", "shop now", "buy now", "product", "catalog", "order now"]
            if not any(sig in corpus for sig in ecommerce_signals):
                print(f"[ecommerce-reject] {candidate.company}: not an active e-commerce store with products")
                report.notes.append(f"[E-Commerce Filter] Rejected {candidate.company}: No e-commerce checkout/cart signals found.")
                continue

        report.log_deep_research()

        if candidate.score < min_heuristic:
            continue

        contact = find_best_contact(
            company=candidate.company,
            website=candidate.website,
            research_text=research_text,
            existing_email=candidate.email,
            config=config,
        )
        if contact:
            candidate.email = contact.email
            report.log_contact()
            if contact.name:
                link = "direct" if contact.direct_match else "business inbox"
                who = f"{contact.name} ({contact.title}) via {link}"
            else:
                who = "business inbox"
            print(f"[contact] {candidate.company}: {who} <{contact.email}> conf={contact.confidence} MX={contact.mx_valid}")
        elif candidate.email:
            # Fallback to public email discovered on website
            from contact_agent import ContactCandidate, _mx_valid
            mx_ok = _mx_valid(candidate.email)
            contact = ContactCandidate(
                email=candidate.email,
                source_url=candidate.website,
                source_text="Public email verified on company domain.",
                confidence=50 if mx_ok else 45,
                mx_valid=mx_ok,
                direct_match=False,
            )
            report.log_contact()
            print(f"[contact-fallback] {candidate.company}: business inbox <{candidate.email}> conf={contact.confidence} MX={mx_ok}")
        elif require_email:
            print(f"[contact-reject] {candidate.company}: no sufficiently supported public business email")
            continue

        ai_result = None
        if use_ai:
            ai_result = qualify_lead(
                company=candidate.company,
                website=candidate.website,
                signal_name=candidate.signal_name,
                signal_score=candidate.score,
                evidence_url=candidate.evidence_url,
                evidence_text=candidate.evidence_text,
                research_text=research_text,
                config=config,
            )
            if ai_result:
                candidate.score = ai_result.score
                if not ai_result.qualified or ai_result.confidence < min_confidence or ai_result.score < min_score:
                    report.log_ai_result(qualified=False, reason=ai_result.reason)
                    print(
                        f"[reject] {candidate.company} score={ai_result.score} "
                        f"confidence={ai_result.confidence}: {ai_result.reason}"
                    )
                    continue
                report.log_ai_result(qualified=True, reason=ai_result.reason)
        elif candidate.score < min_score:
            report.log_ai_result(qualified=False, reason=f"Heuristic score {candidate.score} < {min_score}")
            continue
        else:
            report.log_ai_result(qualified=True, reason=f"Heuristic score {candidate.score}")

        if core.save_candidate(conn, candidate):
            created += 1
            save_enrichment(conn, candidate, research_text, ai_result=ai_result, contact=contact, config=config)
            if ai_result:
                print(
                    f"[{created}] {candidate.company} | {candidate.email or '-'} | "
                    f"AI={ai_result.score}/100 conf={ai_result.confidence}% | {ai_result.pain_summary}"
                )
            else:
                print(f"[{created}] {candidate.company} | {candidate.email or '-'} | heuristic={candidate.score}")

        if i % 5 == 0:
            if report:
                report.save()
            time.sleep(0.5)

    print(f"Saved {created} new qualified lead(s).")
    if should_finish:
        report.finish()
        report.print_summary()
    return report


def personalized_email(row: sqlite3.Row, config: dict) -> tuple[str, str]:
    result = None
    direct_name = row["contact_name"] if row["contact_direct_match"] else ""
    direct_title = row["contact_title"] if row["contact_direct_match"] else ""

    if ai_available(config):
        result = write_email(
            company=row["company"],
            signal_name=row["signal_name"],
            evidence_text=row["evidence_text"] or "",
            pain_summary=row["pain_summary"] or "",
            ai_reason=row["ai_reason"] or "",
            contact_name=direct_name or "",
            contact_title=direct_title or "",
            config=config,
        )

    if result:
        subject, body = result
    else:
        subject, body = core.deterministic_email(row, config)
        if direct_name:
            first = direct_name.split()[0]
            body = body.replace("Hi,", f"Hi {first},", 1)

    if config.get("outreach", {}).get("include_opt_out", True):
        opt_out = "P.S. If this isn't relevant, reply 'no' and I won't follow up."
        if opt_out.lower() not in body.lower():
            body = body.rstrip() + "\n\n" + opt_out
    return subject, body


def outreach(conn: sqlite3.Connection, config: dict, channel: str | None = None,
             report: RunReport | None = None) -> RunReport:
    ensure_extra_columns(conn)
    conn.row_factory = sqlite3.Row

    # 1. Emergency Stop Check
    if stop_controller.is_stop_requested(config):
        print("\n" + "!" * 65)
        print(" [EMERGENCY STOP] Kill Switch is active. All outreach has been HALTED.")
        print("!" * 65 + "\n")
        return report or RunReport(mode="outreach")

    # 2. Channel Resolution
    channel_name, account = core.get_channel_config(config, channel)
    creds_file = account.get("credentials_file")
    token_file = account.get("token_file")
    cap = int(account.get("daily_cap", config.get("outreach", {}).get("daily_cap", 25)))
    delay = float(config.get("outreach", {}).get("delay_seconds", 1.0))
    mode = config.get("outreach", {}).get("send_mode", "draft").lower()
    if mode not in {"draft", "send"}:
        raise ValueError("outreach.send_mode must be 'draft' or 'send'.")

    should_finish = False
    if report is None:
        report = RunReport(mode="outreach", channel_used=channel_name)
        should_finish = True
    else:
        report.channel_used = channel_name

    qualification = config.get("qualification", {})
    min_score = int(qualification.get("min_score", 50))
    require_email = bool(qualification.get("require_public_email", True))
    min_contact_conf = int(config.get("contact_intelligence", {}).get("min_confidence", 45))

    rows = conn.execute(
        """SELECT * FROM leads
           WHERE status='discovered' AND score >= ?
           ORDER BY score DESC, ai_confidence DESC, contact_confidence DESC, created_at ASC LIMIT ?""",
        (min_score, cap),
    ).fetchall()

    if not rows:
        print("No qualified undispatched leads.")
        report.notes.append(f"No undispatched leads found meeting threshold (status='discovered', min_score={min_score}).")
        if should_finish:
            report.finish()
            report.print_summary()
        return report

    try:
        service = core.gmail_service(credentials_path=creds_file, token_path=token_file)
    except Exception as exc:
        print(f"[outreach-auth-error] Gmail service unavailable: {exc}")
        report.notes.append(f"Gmail Error: {exc}")
        report.status = "failed"
        if should_finish:
            report.finish()
            report.print_summary()
        return report
    count = 0
    for row in rows:
        if stop_controller.is_stop_requested(config):
            print("\n[EMERGENCY STOP] Kill switch detected! Halting email dispatch immediately.")
            report.notes.append("Outreach halted early by User Kill Switch.")
            break
        if require_email and not row["email"]:
            continue
        if not row["email"]:
            continue
        if row["contact_confidence"] and row["contact_confidence"] < min_contact_conf:
            continue
        if conn.execute("SELECT 1 FROM suppressions WHERE email=?", (row["email"],)).fetchone():
            continue

        subject, body = personalized_email(row, config)
        report.log_email_personalized()
        payload = {"message": core.build_gmail_message(row["email"], subject, body)}
        target = row["contact_name"] if row["contact_direct_match"] else row["email"]

        try:
            if mode == "send":
                result = service.users().messages().send(userId="me", body=payload["message"]).execute()
                status = "sent"
            else:
                result = service.users().drafts().create(userId="me", body=payload).execute()
                status = "drafted"

            conn.execute(
                "UPDATE leads SET status=?, subject=?, body=?, gmail_message_id=?, updated_at=? WHERE id=?",
                (status, subject, body, result.get("id", ""), core.now_iso(), row["id"]),
            )
            conn.commit()
            count += 1
            report.log_dispatch(success=True, company=row["company"], target=target)
            print(f"[{status}] {row['company']} -> {target} <{row['email']}> score={row['score']}")
        except Exception as exc:
            report.log_dispatch(success=False, company=row["company"], target=target, error=str(exc))
            print(f"[dispatch-error] {row['company']} -> {target}: {exc}")

        time.sleep(delay)

    print(f"Completed: {count} {mode}(s) via channel '{channel_name}'.")
    if should_finish:
        report.finish()
        report.print_summary()
    return report


def stats(conn: sqlite3.Connection) -> None:
    ensure_extra_columns(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status ORDER BY status").fetchall()
    print(f"Total leads: {sum(r['n'] for r in rows)}")
    for row in rows:
        print(f"  {row['status']}: {row['n']}")
    ai_count = conn.execute("SELECT COUNT(*) FROM leads WHERE ai_reason IS NOT NULL AND ai_reason != ''").fetchone()[0]
    named_count = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_name IS NOT NULL AND contact_name != ''").fetchone()[0]
    direct_count = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_direct_match=1").fetchone()[0]
    mx_count = conn.execute("SELECT COUNT(*) FROM leads WHERE email_mx_valid=1").fetchone()[0]
    print(f"AI-qualified: {ai_count}")
    print(f"Named decision-makers found: {named_count}")
    print(f"Direct name-email matches: {direct_count}")
    print(f"MX-valid emails: {mx_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-qualified intent-first outreach engine")
    parser.add_argument("--discover", action="store_true", help="Find, research, contact-enrich and qualify prospects")
    parser.add_argument("--outreach", action="store_true", help="Create drafts or send qualified outreach")
    parser.add_argument("--all", action="store_true", help="Run discovery then outreach")
    parser.add_argument("--channel", type=str, default=None, help="Sending channel/account (e.g., default_gmail, secondary_gmail)")
    parser.add_argument("--gmail-auth", action="store_true", help="Authenticate Gmail OAuth")
    parser.add_argument("--stats", action="store_true", help="Display lead database statistics")
    args = parser.parse_args()

    config = core.load_config()
    conn = core.init_db()
    ensure_extra_columns(conn)

    if args.gmail_auth:
        channel_name, account = core.get_channel_config(config, args.channel)
        creds_file = account.get("credentials_file")
        token_file = account.get("token_file")
        core.gmail_service(credentials_path=creds_file, token_path=token_file)
        print(f"Gmail OAuth ready for channel: {channel_name}")
    elif args.all:
        report = RunReport(mode="all")
        discover(conn, config, report=report)
        outreach(conn, config, channel=args.channel, report=report)
        report.finish()
        report.print_summary()
    elif args.discover:
        discover(conn, config)
    elif args.outreach:
        outreach(conn, config, channel=args.channel)
    elif args.stats:
        stats(conn)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
