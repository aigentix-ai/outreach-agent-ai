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
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import ai_agent
import main as core
from reporter import REPORTS_DIR, RunReport
import smart_main
import stop_controller

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
    stop_controller.clear_stop()
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
            if stop_controller.is_stop_requested(config):
                append_log("Discovery stopped early by Kill Switch.")
            else:
                append_log("Discovery completed successfully.")
        elif action == "outreach":
            smart_main.outreach(conn, config, channel=channel, report=report)
            if stop_controller.is_stop_requested(config):
                append_log("Outreach stopped early by Kill Switch.")
            else:
                append_log("Outreach completed successfully.")
        elif action == "all":
            smart_main.discover(conn, config, report=report)
            if not stop_controller.is_stop_requested(config):
                smart_main.outreach(conn, config, channel=channel, report=report)
            if stop_controller.is_stop_requested(config):
                append_log("Full run stopped early by Kill Switch.")
            else:
                append_log("Full pipeline completed successfully.")
    except BaseException as exc:
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
        if stop_controller.is_stop_requested():
            append_log(f"Action {action.upper()} halted by Emergency Kill Switch.")
        else:
            append_log(f"Action {action.upper()} finished.")


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    conn = core.init_db()
    smart_main.ensure_extra_columns(conn)
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

    try:
        config = core.load_config()
    except Exception:
        config = {}
    ai_ready = os.getenv("GEMINI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or os.getenv("NVIDIA_API_KEY")

    with logs_lock:
        recent_logs = list(task_state["logs"][-30:])

    return {
        "is_running": task_state["is_running"],
        "current_action": task_state["current_action"],
        "started_at": task_state["started_at"],
        "ai_ready": bool(ai_ready),
        "gemini_keys_count": len(ai_agent.get_gemini_api_keys()),
        "gemini_ready": len(ai_agent.get_gemini_api_keys()) > 0,
        "total_leads": total_leads,
        "status_counts": status_counts,
        "ai_qualified_count": ai_count,
        "named_contacts_count": named_count,
        "direct_matches_count": direct_match,
        "mx_valid_count": mx_valid,
        "active_channel": config.get("channels", {}).get("active_channel", "default_gmail"),
        "emergency_stop": stop_controller.is_stop_requested(config),
        "recent_logs": recent_logs,
    }


@app.get("/api/leads")
def get_leads(status: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    conn = core.init_db()
    smart_main.ensure_extra_columns(conn)
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM leads WHERE 1=1"
        params = []

        if status and status != "all":
            query += " AND status = ?"
            params.append(status)

        search = search.strip() if search else None
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


@app.get("/api/leads/export")
def export_leads(status: str | None = None) -> Response:
    import csv
    conn = core.init_db()
    smart_main.ensure_extra_columns(conn)
    conn.row_factory = sqlite3.Row
    try:
        query = "SELECT company, website, email, contact_name, contact_title, score, signal_name, status, subject, body, ai_reason FROM leads"
        params = []
        if status and status != "all":
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY score DESC, created_at DESC"
        rows = conn.execute(query, params).fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Company", "Website", "Email", "Contact Name", "Contact Title", "Score", "Signal", "Status", "Subject", "Email Body", "AI Reason"])
        for r in rows:
            writer.writerow([
                r["company"],
                r["website"],
                r["email"] or "",
                r["contact_name"] or "",
                r["contact_title"] or "",
                r["score"],
                r["signal_name"],
                r["status"],
                r["subject"] or "",
                r["body"] or "",
                r["ai_reason"] or "",
            ])

        csv_data = output.getvalue()
        filename = f"leads_export_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    finally:
        conn.close()


class LeadStatusUpdate(BaseModel):
    status: str


@app.patch("/api/leads/{lead_id}/status")
def update_lead_status(lead_id: int, data: LeadStatusUpdate) -> dict[str, Any]:
    valid_statuses = {"discovered", "drafted", "sent", "archived"}
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status")
    conn = core.init_db()
    try:
        conn.execute("UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (data.status, core.now_iso(), lead_id))
        conn.commit()
        return {"success": True, "lead_id": lead_id, "new_status": data.status}
    finally:
        conn.close()


@app.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: int) -> dict[str, Any]:
    conn = core.init_db()
    try:
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.commit()
        return {"success": True, "message": f"Lead {lead_id} removed"}
    finally:
        conn.close()


@app.post("/api/logs/clear")
def clear_logs() -> dict[str, Any]:
    with logs_lock:
        task_state["logs"] = []
    return {"success": True, "message": "Logs cleared"}


class RateLeadRequest(BaseModel):
    rating: int  # 1 for thumbs up, -1 for thumbs down, or 1 to 5
    feedback_note: str = ""


@app.post("/api/leads/{lead_id}/rate")
def rate_lead(lead_id: int, req: RateLeadRequest) -> dict[str, Any]:
    conn = core.init_db()
    smart_main.ensure_extra_columns(conn)
    try:
        conn.execute(
            "UPDATE leads SET rating = ?, feedback_note = ?, updated_at = ? WHERE id = ?",
            (req.rating, req.feedback_note, core.now_iso(), lead_id),
        )
        conn.commit()
    finally:
        conn.close()
    append_log(f"Lead #{lead_id} rated: {req.rating} (feedback: '{req.feedback_note}')")
    return {"status": "ok", "lead_id": lead_id, "rating": req.rating, "feedback_note": req.feedback_note}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return core.load_config()


class ConfigUpdateRequest(BaseModel):
    offer_name: str
    offer_description: str
    sender_name: str
    cta: str
    target_countries: list[str]
    target_cities: list[str] = []
    target_industries: list[str]
    daily_cap: int
    send_mode: str
    blacklist_keywords: list[str]
    emergency_stop: bool
    discovery_ddg: bool = True
    discovery_meta_ads: bool = True
    discovery_csv_import: bool = False
    discovery_social_radar: bool = False
    discovery_csv_path: str = "leads_input.csv"


@app.post("/api/config/ai-fill")
def ai_fill_config() -> dict[str, Any]:
    """AI automatically decides optimal campaign parameters if fields are blank or requested."""
    config = core.load_config()
    inferred = ai_agent.infer_campaign_settings(config)

    offer = config.setdefault("offer", {})
    offer["name"] = inferred["offer_name"]
    offer["description"] = inferred["offer_description"]
    offer["sender_name"] = inferred["sender_name"]
    offer["cta"] = inferred["cta"]

    target = config.setdefault("target", {})
    target["industries"] = inferred["target_industries"]
    target["countries"] = inferred["target_countries"]
    target["cities"] = inferred["target_cities"]

    filters = config.setdefault("filters", {})
    filters["blacklist_keywords"] = inferred["blacklist_keywords"]

    CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")
    append_log("✨ AI auto-filled optimal settings for AI Commercial Video.")
    return {
        "success": True,
        "message": "AI filled optimal campaign parameters!",
        "inferred": inferred,
        "config": config,
    }


@app.post("/api/config")
def update_config(data: ConfigUpdateRequest) -> dict[str, Any]:
    config = core.load_config()

    # If key fields are left empty, let AI decide the best parameters automatically
    if not data.offer_name.strip() or not data.target_industries or not data.cta.strip():
        inferred = ai_agent.infer_campaign_settings(config)
        if not data.offer_name.strip():
            data.offer_name = inferred["offer_name"]
        if not data.offer_description.strip():
            data.offer_description = inferred["offer_description"]
        if not data.sender_name.strip():
            data.sender_name = inferred["sender_name"]
        if not data.cta.strip():
            data.cta = inferred["cta"]
        if not data.target_industries:
            data.target_industries = inferred["target_industries"]
        if not data.target_countries:
            data.target_countries = inferred["target_countries"]
        if not data.target_cities:
            data.target_cities = inferred["target_cities"]
        if not data.blacklist_keywords:
            data.blacklist_keywords = inferred["blacklist_keywords"]
        append_log("AI automatically populated empty campaign settings.")

    offer = config.setdefault("offer", {})
    offer["name"] = data.offer_name
    offer["description"] = data.offer_description
    offer["sender_name"] = data.sender_name
    offer["cta"] = data.cta

    target = config.setdefault("target", {})
    target["countries"] = data.target_countries
    target["cities"] = data.target_cities
    target["industries"] = data.target_industries

    outreach = config.setdefault("outreach", {})
    outreach["daily_cap"] = data.daily_cap
    outreach["send_mode"] = data.send_mode

    filters = config.setdefault("filters", {})
    filters["blacklist_keywords"] = data.blacklist_keywords
    filters["emergency_stop"] = data.emergency_stop

    # Synchronize stop controller state immediately
    if data.emergency_stop:
        stop_controller.request_stop()
        append_log("🛑 Emergency Kill Switch engaged via Settings.")
    else:
        stop_controller.clear_stop()

    discovery = config.setdefault("discovery", {})
    sources = discovery.setdefault("sources", {})
    sources["duckduckgo"] = data.discovery_ddg
    sources["meta_ads"] = data.discovery_meta_ads
    sources["csv_import"] = data.discovery_csv_import
    sources["social_radar"] = data.discovery_social_radar
    discovery["csv_path"] = data.discovery_csv_path

    CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")
    try:
        example_path = ROOT / "config.example.yaml"
        if example_path.exists():
            example_path.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")
    except Exception:
        pass
    append_log("Settings updated successfully via Dashboard.")
    return {"success": True, "message": "Settings saved", "config": config}


class ThemeUpdateRequest(BaseModel):
    theme: str


@app.get("/api/theme")
def get_theme(request: Request) -> dict[str, str]:
    cookie_theme = request.cookies.get("theme")
    if cookie_theme in {"light", "dark"}:
        return {"theme": cookie_theme}
    try:
        config = core.load_config()
        theme = config.get("theme", "dark")
        return {"theme": theme}
    except Exception:
        return {"theme": "dark"}


@app.post("/api/theme")
def set_theme(data: ThemeUpdateRequest, response: Response) -> dict[str, Any]:
    theme = "light" if data.theme == "light" else "dark"
    try:
        config = core.load_config()
        config["theme"] = theme
        CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")
    except Exception as e:
        logging.warning(f"Could not persist theme to config: {e}")

    response.set_cookie(
        key="theme",
        value=theme,
        max_age=31536000,
        httponly=False,
        samesite="lax",
    )
    return {"success": True, "theme": theme}


@app.post("/api/action/stop")
def stop_pipeline_action() -> dict[str, Any]:
    stop_controller.request_stop()
    curr = task_state.get("current_action")
    task_state["is_running"] = False
    task_state["current_action"] = None
    append_log("🛑 EMERGENCY KILL SWITCH ACTIVATED! Halting all running tasks immediately...")
    return {
        "success": True,
        "message": f"Emergency Kill Switch activated. Stopped {curr or 'active task'}.",
    }


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
    clean_id = "".join(c for c in report_id if c.isalnum() or c in "_-")
    json_path = REPORTS_DIR / f"report_{clean_id}.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    md_path = REPORTS_DIR / f"report_{clean_id}.md"
    markdown = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    return {
        "id": clean_id,
        "filename": json_path.name,
        "data": data,
        "markdown": markdown,
    }


@app.delete("/api/reports/{report_id}")
def delete_report(report_id: str) -> dict[str, Any]:
    clean_id = "".join(c for c in report_id if c.isalnum() or c in "_-")
    json_path = REPORTS_DIR / f"report_{clean_id}.json"
    md_path = REPORTS_DIR / f"report_{clean_id}.md"
    deleted = False
    if json_path.exists():
        json_path.unlink()
        deleted = True
    if md_path.exists():
        md_path.unlink()
        deleted = True
    if not deleted:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"success": True, "message": f"Report {clean_id} deleted"}


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
def index(request: Request) -> str:
    html_path = ROOT / "static" / "index.html"
    if html_path.exists():
        content = html_path.read_text(encoding="utf-8")
        cookie_theme = request.cookies.get("theme")
        if not cookie_theme:
            try:
                config = core.load_config()
                cookie_theme = config.get("theme", "dark")
            except Exception:
                cookie_theme = "dark"
        if cookie_theme == "light":
            content = content.replace('<html lang="en" class="dark">', '<html lang="en">')
        else:
            if '<html lang="en" class="dark">' not in content:
                content = content.replace('<html lang="en">', '<html lang="en" class="dark">')
        return content
    return "<h1>Outreach Dashboard Initializing...</h1>"


if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
