from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"


@dataclass
class RunReport:
    run_id: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    mode: str = "run"
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str = ""
    prospects_scanned: int = 0
    prospects_blacklisted: int = 0
    deep_researches_conducted: int = 0
    contacts_found: int = 0
    ai_qualified: int = 0
    ai_rejected: int = 0
    emails_personalized: int = 0
    dispatched_success: int = 0
    dispatched_failed: int = 0
    channel_used: str = ""
    failures: list[dict] = field(default_factory=list)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    status: str = "in_progress"
    notes: list[str] = field(default_factory=list)

    def log_scan(self, count: int) -> None:
        self.prospects_scanned += count

    def log_blacklist(self, company: str, reason: str) -> None:
        self.prospects_blacklisted += 1
        self.notes.append(f"[Blacklisted] {company}: {reason}")

    def log_deep_research(self) -> None:
        self.deep_researches_conducted += 1

    def log_contact(self) -> None:
        self.contacts_found += 1

    def log_ai_result(self, qualified: bool, reason: str = "") -> None:
        if qualified:
            self.ai_qualified += 1
        else:
            self.ai_rejected += 1
            short_reason = (reason[:80] + "...") if len(reason) > 80 else reason
            self.rejection_reasons[short_reason] = self.rejection_reasons.get(short_reason, 0) + 1

    def log_email_personalized(self) -> None:
        self.emails_personalized += 1

    def log_dispatch(self, success: bool, company: str, target: str, error: str = "") -> None:
        if success:
            self.dispatched_success += 1
        else:
            self.dispatched_failed += 1
            self.failures.append({"company": company, "target": target, "error": error})

    def finish(self, status: str = "completed") -> None:
        self.status = status
        self.finished_at = datetime.now(timezone.utc).isoformat()
        self.save()

    def print_summary(self) -> None:
        duration = ""
        if self.started_at and self.finished_at:
            try:
                t1 = datetime.fromisoformat(self.started_at)
                t2 = datetime.fromisoformat(self.finished_at)
                duration = f" ({int((t2 - t1).total_seconds())}s)"
            except Exception:
                pass

        print("\n" + "=" * 65)
        print(f"         OUTREACH AGENT - EXECUTION SUMMARY REPORT{duration}")
        print("=" * 65)
        print(f"Run ID:                {self.run_id}")
        print(f"Mode:                  {self.mode.upper()}")
        print(f"Status:                {self.status.upper()}")
        print(f"Channel Used:          {self.channel_used or 'Default'}")
        print(f"Prospects Scanned:     {self.prospects_scanned}")
        print(f"Filtered / Blacklist:  {self.prospects_blacklisted}")
        print(f"Deep Researches Done:  {self.deep_researches_conducted}")
        print(f"Contacts Identified:   {self.contacts_found}")
        print(f"AI Qualified:          {self.ai_qualified}")
        print(f"AI Rejected:           {self.ai_rejected}")
        print(f"Emails Personalized:   {self.emails_personalized}")
        print(f"Dispatched Success:    {self.dispatched_success}")
        print(f"Dispatched Failures:   {self.dispatched_failed}")

        if self.rejection_reasons:
            print("\nTop AI Rejection Reasons:")
            for reason, count in sorted(self.rejection_reasons.items(), key=lambda x: x[1], reverse=True)[:5]:
                print(f"  - [{count}x] {reason}")

        if self.failures:
            print(f"\nDispatches Failed ({len(self.failures)}):")
            for f in self.failures[:5]:
                print(f"  - {f.get('company')} ({f.get('target')}): {f.get('error')}")

        print("=" * 65 + "\n")

    def save(self) -> tuple[Path, Path]:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        json_path = REPORTS_DIR / f"report_{self.run_id}.json"
        md_path = REPORTS_DIR / f"report_{self.run_id}.md"

        data = asdict(self)
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        md_content = f"""# Outreach Agent Run Report

- **Run ID**: `{self.run_id}`
- **Mode**: `{self.mode.upper()}`
- **Status**: `{self.status.upper()}`
- **Channel**: `{self.channel_used or 'Default'}`
- **Started**: `{self.started_at}`
- **Finished**: `{self.finished_at}`

## Funnel Performance

| Step | Count |
| :--- | :--- |
| **Prospects Scanned** | {self.prospects_scanned} |
| **Blacklisted / Excluded** | {self.prospects_blacklisted} |
| **Deep Researches** | {self.deep_researches_conducted} |
| **Contacts Discovered** | {self.contacts_found} |
| **AI Qualified** | {self.ai_qualified} |
| **AI Rejected** | {self.ai_rejected} |
| **Emails Personalized** | {self.emails_personalized} |
| **Outreach Successful** | {self.dispatched_success} |
| **Outreach Failed** | {self.dispatched_failed} |

"""
        if self.rejection_reasons:
            md_content += "## AI Rejection Breakdown\n\n"
            for reason, count in sorted(self.rejection_reasons.items(), key=lambda x: x[1], reverse=True):
                md_content += f"- **[{count}x]** {reason}\n"
            md_content += "\n"

        if self.failures:
            md_content += "## Dispatches Failed\n\n"
            for fail in self.failures:
                md_content += f"- **{fail.get('company')}** ({fail.get('target')}): {fail.get('error')}\n"
            md_content += "\n"

        if self.notes:
            md_content += "## Activity Notes\n\n"
            for note in self.notes[:30]:
                md_content += f"- {note}\n"

        md_path.write_text(md_content, encoding="utf-8")
        return json_path, md_path
