from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from darkroom.triage import db as triage_db
from darkroom.triage.actions import copy_corrected, move, rename, trash, revert
from darkroom.triage.preview import generate_thumbnail

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates" / "triage"

_CATEGORIES = [
    "flat_restructure",
    "calibration_in_target",
    "legacy_session",
    "processed_dir",
    "thumbnail_cleanup",
    "missing_object",
    "ra_dec_mismatch",
]


def create_app(*, db_path: Path, archive_root: Path) -> FastAPI:
    app = FastAPI(title="darkroom triage")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    cache_dir = archive_root / ".triage_cache"
    trash_root = archive_root / ".triage_trash"
    cache_dir.mkdir(exist_ok=True)

    app.mount(
        "/thumbnails",
        StaticFiles(directory=str(cache_dir), check_dir=False),
        name="thumbnails",
    )

    def _conn():
        return triage_db.open_db(db_path)

    def _counts(conn):
        total = triage_db.count_items(conn)
        done = triage_db.count_items(conn, status="applied")
        by_cat = {
            cat: {
                "pending": triage_db.count_items(conn, category=cat, status="pending"),
                "approved": triage_db.count_items(conn, category=cat, status="approved"),
                "skipped": triage_db.count_items(conn, category=cat, status="skipped"),
                "applied": triage_db.count_items(conn, category=cat, status="applied"),
            }
            for cat in _CATEGORIES
        }
        return {"total": total, "done": done, "by_cat": by_cat}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        conn = _conn()
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"counts": _counts(conn)},
        )

    @app.get("/queue", response_class=HTMLResponse)
    def queue(
        request: Request,
        category: str | None = None,
        status: str | None = None,
        offset: int = 0,
    ):
        conn = _conn()
        items = triage_db.list_items(
            conn, category=category, status=status, limit=50, offset=offset
        )
        return templates.TemplateResponse(
            request,
            "queue.html",
            {
                "items": items,
                "category": category,
                "status": status,
                "offset": offset,
                "categories": _CATEGORIES,
            },
        )

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_detail(request: Request, item_id: int):
        conn = _conn()
        item = triage_db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")

        thumbnail_url = None
        meta = item.get("fits_metadata") or {}
        sample = meta.get("sample_file")
        if sample and Path(sample).exists():
            try:
                jpg = generate_thumbnail(Path(sample), cache_dir)
                thumbnail_url = f"/thumbnails/{jpg.name}"
            except Exception:
                pass

        rows = triage_db.list_items(conn, status="pending", limit=2)
        next_id = next(
            (r["id"] for r in rows if r["id"] != item_id), None
        )

        return templates.TemplateResponse(
            request,
            "item.html",
            {
                "item": item,
                "thumbnail_url": thumbnail_url,
                "next_id": next_id,
            },
        )

    @app.post("/item/{item_id}/approve")
    def approve_item(
        item_id: int,
        proposed_path: str = Form(default=""),
        proposed_value: str = Form(default=""),
        user_notes: str = Form(default=""),
    ):
        conn = _conn()
        item = triage_db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404)
        status = "modified" if (proposed_path or proposed_value) else "approved"
        triage_db.update_status(
            conn,
            item_id,
            status,
            user_notes=user_notes or None,
            proposed_path=proposed_path or None,
            proposed_value=proposed_value or None,
        )
        rows = triage_db.list_items(conn, status="pending", limit=1)
        if rows:
            return RedirectResponse(f"/item/{rows[0]['id']}", status_code=303)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/item/{item_id}/skip")
    def skip_item(item_id: int, user_notes: str = Form(default="")):
        conn = _conn()
        triage_db.update_status(conn, item_id, "skipped",
                                 user_notes=user_notes or None)
        rows = triage_db.list_items(conn, status="pending", limit=1)
        if rows:
            return RedirectResponse(f"/item/{rows[0]['id']}", status_code=303)
        return RedirectResponse("/queue", status_code=303)

    @app.post("/item/{item_id}/flag")
    def flag_item(item_id: int, user_notes: str = Form(default="")):
        conn = _conn()
        triage_db.update_status(conn, item_id, "pending",
                                 user_notes=user_notes or None)
        return RedirectResponse(f"/item/{item_id}", status_code=303)

    @app.get("/commit", response_class=HTMLResponse)
    def commit_page(request: Request):
        conn = _conn()
        approved = triage_db.list_items(conn, status="approved", limit=500)
        modified = triage_db.list_items(conn, status="modified", limit=500)
        return templates.TemplateResponse(
            request,
            "commit.html",
            {"items": approved + modified},
        )

    @app.post("/commit/execute")
    def commit_execute():
        """Stream SSE progress as approved items are applied."""

        def generate():
            conn = _conn()
            items = (
                triage_db.list_items(conn, status="approved", limit=500)
                + triage_db.list_items(conn, status="modified", limit=500)
            )
            for item in items:
                item_id = item["id"]
                src = Path(item["source_path"])
                dst = Path(item["proposed_path"]) if item["proposed_path"] else None
                cat = item["category"]
                try:
                    if cat == "thumbnail_cleanup":
                        trash(conn, item_id, src,
                              archive_root=archive_root, trash_root=trash_root)
                    elif cat in ("flat_restructure", "processed_dir", "legacy_session"):
                        rename(conn, item_id, src, dst)
                    elif cat == "calibration_in_target":
                        move(conn, item_id, src, dst)
                    elif cat in ("missing_object", "ra_dec_mismatch"):
                        patches = {}
                        if item.get("proposed_value"):
                            patches["OBJECT"] = item["proposed_value"]
                        copy_corrected(conn, item_id, src, dst, patches)
                    triage_db.update_status(conn, item_id, "applied")
                    yield f"data: {json.dumps({'id': item_id, 'result': 'success'})}\n\n"
                except Exception as exc:
                    triage_db.update_status(conn, item_id, "error")
                    yield f"data: {json.dumps({'id': item_id, 'result': 'error', 'msg': str(exc)})}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, offset: int = 0):
        conn = _conn()
        entries = triage_db.list_audit(conn, limit=100, offset=offset)
        return templates.TemplateResponse(
            request,
            "audit.html",
            {"entries": entries, "offset": offset},
        )

    @app.post("/audit/{log_id}/revert")
    def revert_action(log_id: int):
        conn = _conn()
        entry = triage_db.get_audit_entry(conn, log_id)
        if entry is None:
            raise HTTPException(status_code=404)
        revert(conn, log_id, trash_root=trash_root)
        return RedirectResponse("/audit", status_code=303)

    @app.get("/audit/export.csv")
    def export_csv():
        conn = _conn()
        entries = triage_db.list_audit(conn, limit=10000)
        lines = ["id,triage_item_id,action_type,source_path,dest_path,result,applied_at,reverted_at"]
        for e in entries:
            lines.append(
                f"{e['id']},{e['triage_item_id']},{e['action_type']},"
                f"\"{e['source_path']}\",\"{e['dest_path']}\","
                f"{e['result'] or ''},{e['applied_at']},{e['reverted_at'] or ''}"
            )
        csv_text = "\n".join(lines)
        return StreamingResponse(
            iter([csv_text]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
        )

    return app
