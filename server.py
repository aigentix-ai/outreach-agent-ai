from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import main as core
import smart_main
from reporter import REPORTS_DIR, RunReport

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "leads.db"
CONFIG_PATH = ROOT / "config.yaml"
load_dotenv(ROOT / ".env")

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

app = FastAPI(title="OutreachAgent Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global execution state & live logs
task_state = {
    "is_running": False,
    "current_action": None,
    "started_at": None,
    "logs": [],
}
logs_lock = threading.Lock()


def append_log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with logs_lock:
        task_state["logs"].append(f"[{timestamp}] {message}")
        if len(task_state["logs"]) > 200:
            task_state["logs"].pop(0)


class OutputInterceptor(io.TextIOBase):
    def __init__(self, original_stdout):
        self.original = original_stdout

    def write(self, s: str) -> int:
        if s.strip():
            append_log(s.strip())
        try:
            return self.original.write(s)
        except Exception:
            try:
                safe_s = s.encode("ascii", errors="replace").decode("ascii")
                return self.original.write(safe_s)
            except Exception:
                return len(s)

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass


def run_pipeline_worker(action: str, channel: str | None = None) -> None:
    task_state["is_running"] = True
    task_state["current_action"] = action
    task_state["started_at"] = datetime.now(timezone.utc).isoformat()
    append_log(f"Starting action: {action.upper()}...")

    old_stdout = sys.stdout
    sys.stdout = OutputInterceptor(old_stdout)

    report = RunReport(mode=action)
    report.save()
    conn = None
    try:
        config = core.load_config()
        conn = core.init_db()
        smart_main.ensure_extra_columns(conn)

        if action == "discover":
            smart_main.discover(conn, config, report=report)
            append_log("Discovery completed successfully.")
        elif action == "outreach":
            smart_main.outreach(conn, config, channel=channel, report=report)
            append_log("Outreach completed successfully.")
        elif action == "all":
            smart_main.discover(conn, config, report=report)
            smart_main.outreach(conn, config, channel=channel, report=report)
            append_log("Full pipeline completed successfully.")
    except Exception as exc:
        report.notes.append(f"Execution Error: {str(exc)}")
        report.status = "failed"
        append_log(f"ERROR: {str(exc)}")
    finally:
        try:
            report.finish()
            append_log(f"Saved execution report: report_{report.run_id}")
        except Exception as rep_err:
            append_log(f"Failed to save report: {rep_err}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        sys.stdout = old_stdout
        task_state["is_running"] = False
        task_state["current_action"] = None
        append_log(f"Action {action.upper()} finished.")


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    conn = core.init_db()
    conn.row_factory = sqlite3.Row
    try:
        status_counts = {}
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status").fetchall()
        total_leads = sum(r["n"] for r in rows)
        for r in rows:
            status_counts[r["status"]] = r["n"]

        ai_count = conn.execute("SELECT COUNT(*) FROM leads WHERE ai_reason IS NOT NULL AND ai_reason != ''").fetchone()[0]
        named_count = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_name IS NOT NULL AND contact_name != ''").fetchone()[0]
        direct_match = conn.execute("SELECT COUNT(*) FROM leads WHERE contact_direct_match=1").fetchone()[0]
        mx_valid = conn.execute("SELECT COUNT(*) FROM leads WHERE email_mx_valid=1").fetchone()[0]
    finally:
        conn.close()

    config = core.load_config()
    ai_ready = os.getenv("GEMINI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("NVIDIA_API_KEY")

    with logs_lock:
        recent_logs = list(task_state["logs"][-30:])

    return {
        "is_running": task_state["is_running"],
        "current_action": task_state["current_action"],
        "ai_ready": bool(ai_ready),
        "total_leads": total_leads,
        "status_counts": status_counts,
        "ai_qualified_count": ai_count,
        "named_contacts_count": named_count,
        "direct_matches_count": direct_match,
        "mx_valid_count": mx_valid,
        "active_channel": config.get("channels", {}).get("active_channel", "default_gmail"),
        "emergency_stop": config.get("filters", {}).get("emergency_stop", False),
        "recent_logs": recent_logs,
    }


@app.get("/api/leads")
def get_leads(status: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    conn = core.init_db()
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM leads WHERE 1=1"
        params = []

        if status and status != "all":
            query += " AND status = ?"
            params.append(status)

        if search:
            query += " AND (company LIKE ? OR email LIKE ? OR website LIKE ?)"
            s = f"%{search}%"
            params.extend([s, s, s])

        query += " ORDER BY score DESC, created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        leads = [dict(r) for r in rows]

        total_query = "SELECT COUNT(*) FROM leads WHERE 1=1"
        t_params = []
        if status and status != "all":
            total_query += " AND status = ?"
            t_params.append(status)
        if search:
            total_query += " AND (company LIKE ? OR email LIKE ? OR website LIKE ?)"
            s = f"%{search}%"
            t_params.extend([s, s, s])
        total = conn.execute(total_query, t_params).fetchone()[0]
    finally:
        conn.close()

    return {"total": total, "leads": leads}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return core.load_config()


class ConfigUpdateRequest(BaseModel):
    offer_name: str
    offer_description: str
    sender_name: str
    cta: str
    target_countries: list[str]
    target_industries: list[str]
    daily_cap: int
    send_mode: str
    blacklist_keywords: list[str]
    emergency_stop: bool


@app.post("/api/config")
def update_config(data: ConfigUpdateRequest) -> dict[str, Any]:
    config = core.load_config()
    config["offer"]["name"] = data.offer_name
    config["offer"]["description"] = data.offer_description
    config["offer"]["sender_name"] = data.sender_name
    config["offer"]["cta"] = data.cta

    config["target"]["countries"] = data.target_countries
    config["target"]["industries"] = data.target_industries

    config["outreach"]["daily_cap"] = data.daily_cap
    config["outreach"]["send_mode"] = data.send_mode

    if "filters" not in config:
        config["filters"] = {}
    config["filters"]["blacklist_keywords"] = data.blacklist_keywords
    config["filters"]["emergency_stop"] = data.emergency_stop

    CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")
    append_log("Settings updated successfully via Dashboard.")
    return {"success": True, "message": "Settings saved"}


@app.post("/api/action/{action_name}")
def trigger_action(action_name: str, background_tasks: BackgroundTasks, channel: str | None = None) -> dict[str, Any]:
    if action_name not in {"discover", "outreach", "all"}:
        raise HTTPException(status_code=400, detail="Invalid action name")
    if task_state["is_running"]:
        return {"success": False, "message": f"Task already running: {task_state['current_action']}"}

    thread = threading.Thread(target=run_pipeline_worker, args=(action_name, channel), daemon=True)
    thread.start()
    return {"success": True, "message": f"Started {action_name.upper()} in background."}


@app.get("/api/reports")
def get_reports() -> list[dict[str, Any]]:
    if not REPORTS_DIR.exists():
        return []
    reports = []
    for p in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            md_path = p.with_suffix(".md")
            markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
            reports.append({
                "id": p.stem.replace("report_", ""),
                "filename": p.name,
                "data": data,
                "markdown": markdown,
            })
        except Exception:
            pass
    return reports[:50]


@app.get("/api/reports/{report_id}")
def get_report_detail(report_id: str) -> dict[str, Any]:
    json_path = REPORTS_DIR / f"report_{report_id}.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    md_path = REPORTS_DIR / f"report_{report_id}.md"
    markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    return {
        "id": report_id,
        "filename": json_path.name,
        "data": data,
        "markdown": markdown,
    }


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: str) -> dict[str, Any]:
    json_path = REPORTS_DIR / f"report_{report_id}.json"
    md_path = REPORTS_DIR / f"report_{report_id}.md"
    deleted = False
    if json_path.exists():
        json_path.unlink()
        deleted = True
    if md_path.exists():
        md_path.unlink()
        deleted = True
    if not deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"success": True, "message": f"Report {report_id} deleted"}


@app.delete("/api/reports")
def clear_all_reports() -> dict[str, Any]:
    count = 0
    if REPORTS_DIR.exists():
        for p in list(REPORTS_DIR.glob("report_*.*")):
            try:
                p.unlink()
                count += 1
            except Exception:
                pass
    return {"success": True, "message": f"Cleared {count} report files"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    html_path = ROOT / "static" / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>Outreach Dashboard Initializing...</h1>"


if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
