from __future__ import annotations

import asyncio
import html
from urllib.parse import quote
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .ai import AIAnalyzer
from .config import Settings, settings
from .folk import FolkClient, FolkRateLimitError
from .models import AIAnalysis, normalize_kondo_payload
from .store import SyncStore


def create_app(app_settings: Settings = settings) -> FastAPI:
    store = SyncStore(app_settings.database_path)
    analyzer = AIAnalyzer(app_settings)
    folk = FolkClient(app_settings, store)
    reconcile_task: asyncio.Task[None] | None = None
    worker_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal reconcile_task, worker_task
        if app_settings.worker_enabled:
            worker_task = asyncio.create_task(
                _worker_loop(
                    app_settings.worker_interval_seconds,
                    app_settings.worker_batch_size,
                    store,
                    analyzer,
                    folk,
                )
            )
        if app_settings.reconcile_interval_minutes > 0:
            reconcile_task = asyncio.create_task(
                _reconcile_loop(app_settings.reconcile_interval_minutes, store, analyzer, folk)
            )
        try:
            yield
        finally:
            if reconcile_task:
                reconcile_task.cancel()
            if worker_task:
                worker_task.cancel()

    app = FastAPI(title="Kondo to folk AI Sync", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "dry_run": app_settings.dry_run,
            "ai_provider": app_settings.ai_provider,
            "database": str(app_settings.database_path),
            "reconcile_interval_minutes": app_settings.reconcile_interval_minutes,
            "worker_enabled": app_settings.worker_enabled,
            "review_mode": app_settings.review_mode,
            "queue_depth": store.queue_depth(app_settings.processing_timeout_seconds),
            "processing_timeout_seconds": app_settings.processing_timeout_seconds,
        }

    @app.get("/events")
    async def events(limit: int = 50) -> dict[str, Any]:
        return {"items": store.recent_events(limit=min(limit, 100))}

    @app.get("/console", response_class=HTMLResponse)
    async def console(
        request: Request,
        token: str | None = None,
        notice: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> HTMLResponse:
        _require_admin(app_settings, token or x_admin_token)
        return HTMLResponse(_console_html(app_settings, store, token, notice))

    @app.get("/admin/stats")
    async def admin_stats(
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_admin(app_settings, token or x_admin_token)
        return {
            "health": {
                "ok": True,
                "dry_run": app_settings.dry_run,
                "ai_provider": app_settings.ai_provider,
                "database": str(app_settings.database_path),
                "reconcile_interval_minutes": app_settings.reconcile_interval_minutes,
                "worker_enabled": app_settings.worker_enabled,
                "review_mode": app_settings.review_mode,
                "queue_depth": store.queue_depth(app_settings.processing_timeout_seconds),
                "processing_timeout_seconds": app_settings.processing_timeout_seconds,
            },
            "counts": store.status_counts(),
            "recent_events": store.recent_events(limit=25),
            "retryable_events": store.retryable_events(
                limit=25,
                processing_timeout_seconds=app_settings.processing_timeout_seconds,
            ),
        }

    @app.get("/admin/priority")
    async def admin_priority(
        limit: int = 25,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_admin(app_settings, token or x_admin_token)
        return {"items": store.priority_events(limit=min(limit, 100))}

    @app.get("/admin/triage")
    async def admin_triage(
        limit: int = 100,
        since_hours: int = 0,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_admin(app_settings, token or x_admin_token)
        return {
            "items": store.triage_events(
                limit=min(limit, 250),
                since_hours=max(0, min(since_hours, 168)),
            )
        }

    @app.post("/admin/reprocess/{idempotency_key}", response_model=None)
    async def admin_reprocess(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        payload = store.get_event_payload(idempotency_key)
        if payload is None:
            raise HTTPException(status_code=404, detail="Event not found")
        result = _enqueue_payload(payload, store, force=True)
        return _action_response(request, token, result, "Requeued one stored payload.")

    @app.post("/admin/stage/{idempotency_key}", response_model=None)
    async def admin_stage(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.stage_for_folk(idempotency_key)
        result = {"status": "staged_for_folk", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Staged one row for the batch.")

    @app.post("/admin/stage-all", response_model=None)
    async def admin_stage_all(
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        count = store.stage_all_for_folk()
        result = {"status": "staged_for_folk", "count": count}
        return _action_response(request, token, result, f"Staged {count} pending row(s).")

    @app.post("/admin/stage-selected", response_model=None)
    async def admin_stage_selected(
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        form = await request.form()
        selected = [str(value) for value in form.getlist("selected")]
        count = store.stage_many_for_folk(selected)
        result = {"status": "staged_for_folk", "count": count}
        return _action_response(request, token, result, f"Staged {count} selected row(s).")

    @app.post("/admin/send-staged", response_model=None)
    async def admin_send_staged(
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        count = store.queue_staged_for_folk()
        result = {"status": "queued_for_folk", "count": count}
        return _action_response(request, token, result, f"Queued {count} staged row(s) for folk.")

    @app.post("/admin/unstage/{idempotency_key}", response_model=None)
    async def admin_unstage(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.unstage_for_folk(idempotency_key)
        result = {"status": "unstaged", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Removed one row from the send batch.")

    @app.post("/admin/request-full-sync/{idempotency_key}", response_model=None)
    async def admin_request_full_sync(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.request_full_sync(idempotency_key)
        result = {"status": "full_sync_requested", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Marked one row for full-history sync.")

    @app.post("/admin/reset-local-state", response_model=None)
    async def admin_reset_local_state(
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        form = await request.form()
        if str(form.get("confirm") or "") != "RESET":
            raise HTTPException(status_code=400, detail="Type RESET to clear local sync state")
        store.reset_all()
        result = {"status": "reset", "message": "local sync state cleared"}
        return _action_response(request, token, result, "Cleared local sync state.")

    @app.post("/admin/reconcile", response_model=None)
    async def admin_reconcile(
        request: Request,
        limit: int = 25,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        result = await _process_queue(
            store,
            analyzer,
            folk,
            limit=min(limit, 100),
            processing_timeout_seconds=app_settings.processing_timeout_seconds,
        )
        return _action_response(request, token, result, f"Retried {result['count']} queued/retry item(s).")

    @app.post("/admin/process", response_model=None)
    async def admin_process(
        request: Request,
        limit: int = 25,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        result = await _process_queue(
            store,
            analyzer,
            folk,
            limit=min(limit, 100),
            processing_timeout_seconds=app_settings.processing_timeout_seconds,
        )
        return _action_response(request, token, result, f"Processed {result['count']} queued item(s).")

    @app.post("/webhooks/kondo")
    async def kondo_webhook(
        request: Request,
        x_kondo_webhook_secret: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        provided_secret = x_kondo_webhook_secret or x_api_key
        if app_settings.webhook_secret and provided_secret != app_settings.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object")
        return _enqueue_payload(payload, store)

    @app.post("/sync/manual")
    async def manual_sync(payload: dict[str, Any]) -> dict[str, Any]:
        return _enqueue_payload(payload, store)

    return app


def _enqueue_payload(
    payload: dict[str, Any],
    store: SyncStore,
    force: bool = False,
) -> dict[str, Any]:
    event = normalize_kondo_payload(payload)
    existing = store.get_event(event.idempotency_key)
    if (
        existing
        and not force
        and event.has_full_history
        and str(existing.get("status") or "") in {"review_pending", "full_sync_requested", "staged_for_folk"}
    ):
        force = True
    if existing and not force and _should_skip_existing(existing["status"], event.linkedin_url, dry_run=False):
        return {
            "status": "duplicate",
            "idempotency_key": event.idempotency_key,
            "previous_status": existing["status"],
        }
    queued = store.queue_event(event.idempotency_key, event.linkedin_url, event.to_dict(), force=force)
    return {
        "status": queued["status"],
        "idempotency_key": event.idempotency_key,
        "linkedin_url": event.linkedin_url,
    }


async def _process_payload(
    payload: dict[str, Any],
    store: SyncStore,
    analyzer: AIAnalyzer,
    folk: FolkClient,
    force: bool = False,
    bypass_review: bool = False,
) -> dict[str, Any]:
    event = normalize_kondo_payload(payload)
    existing = store.get_event(event.idempotency_key)
    if existing and force:
        store.delete_event(event.idempotency_key)
        existing = None
    if (
        existing
        and not bypass_review
        and _should_skip_existing(existing["status"], event.linkedin_url, folk.settings.dry_run)
    ):
        return {
            "status": "duplicate",
            "idempotency_key": event.idempotency_key,
            "previous_status": existing["status"],
        }

    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    try:
        if not event.linkedin_url:
            result = {"status": "held_for_review", "reason": "missing_linkedin_url"}
            store.finish_event(event.idempotency_key, "held_for_review", result=result)
            return {"idempotency_key": event.idempotency_key, **result}

        stored_analysis = store.get_event_analysis(event.idempotency_key)
        if bypass_review and stored_analysis:
            analysis = AIAnalysis.from_dict(stored_analysis)
        else:
            analysis = await analyzer.analyze(event)
        if analysis.relationship_stage == "not_relevant":
            result = {
                "status": "excluded",
                "reason": "not_relevant_to_prospecting",
                "group_reason": analysis.group_reason,
            }
            store.finish_event(
                event.idempotency_key,
                "excluded",
                analysis=analysis.to_dict(),
                result=result,
            )
            return {
                "idempotency_key": event.idempotency_key,
                "analysis": analysis.to_dict(),
                "result": result,
            }
        if folk.settings.review_mode and not bypass_review:
            result = {
                "status": "review_pending",
                "reason": "awaiting_console_approval",
                "has_full_history": event.has_full_history,
            }
            store.finish_event(
                event.idempotency_key,
                "review_pending",
                analysis=analysis.to_dict(),
                result=result,
            )
            return {
                "idempotency_key": event.idempotency_key,
                "analysis": analysis.to_dict(),
                "result": result,
            }
        result = await folk.sync(event, analysis)
        status = str(result.get("status") or "synced")
        store.finish_event(
            event.idempotency_key,
            status,
            analysis=analysis.to_dict(),
            result=result,
        )
        return {
            "idempotency_key": event.idempotency_key,
            "analysis": analysis.to_dict(),
            "result": result,
        }
    except FolkRateLimitError as exc:
        store.defer_event(event.idempotency_key, str(exc), exc.retry_at)
        return {
            "idempotency_key": event.idempotency_key,
            "result": {
                "status": "retry_wait",
                "retry_at": exc.retry_at.astimezone(UTC).isoformat(),
                "reason": str(exc),
            },
        }
    except Exception as exc:
        store.finish_event(event.idempotency_key, "error", error=str(exc))
        raise


def _should_skip_existing(status: str, linkedin_url: str | None, dry_run: bool) -> bool:
    if status == "synced":
        return True
    if status == "dry_run":
        return dry_run
    if status == "held_for_review":
        return not bool(linkedin_url)
    if status == "excluded":
        return True
    if status in {"review_pending", "full_sync_requested", "staged_for_folk", "queued_for_folk"}:
        return True
    return False


async def _reconcile_loop(
    interval_minutes: int,
    store: SyncStore,
    analyzer: AIAnalyzer,
    folk: FolkClient,
) -> None:
    while True:
        await asyncio.sleep(interval_minutes * 60)
        await _process_queue(
            store,
            analyzer,
            folk,
            limit=25,
            processing_timeout_seconds=folk.settings.processing_timeout_seconds,
        )


async def _worker_loop(
    interval_seconds: float,
    batch_size: int,
    store: SyncStore,
    analyzer: AIAnalyzer,
    folk: FolkClient,
) -> None:
    while True:
        await _process_queue(
            store,
            analyzer,
            folk,
            limit=max(1, batch_size),
            processing_timeout_seconds=folk.settings.processing_timeout_seconds,
        )
        await asyncio.sleep(max(1.0, interval_seconds))


async def _process_queue(
    store: SyncStore,
    analyzer: AIAnalyzer,
    folk: FolkClient,
    limit: int = 25,
    processing_timeout_seconds: int = 120,
) -> dict[str, Any]:
    attempted: list[dict[str, Any]] = []
    for _ in range(limit):
        event = store.next_queued_event(processing_timeout_seconds=processing_timeout_seconds)
        if event is None:
            break
        idempotency_key = str(event["idempotency_key"])
        payload = store.get_event_payload(idempotency_key)
        if payload is None:
            attempted.append(
                {
                    "idempotency_key": idempotency_key,
                    "status": "missing_payload",
                }
            )
            continue
        store.mark_processing(idempotency_key)
        try:
            result = await _process_payload(
                payload,
                store,
                analyzer,
                folk,
                bypass_review=str(event.get("status") or "") == "queued_for_folk",
            )
            status = result.get("result", {}).get("status") or result.get("status")
            attempted.append({"idempotency_key": idempotency_key, "status": status})
            if status == "retry_wait":
                break
        except Exception as exc:
            attempted.append(
                {
                    "idempotency_key": idempotency_key,
                    "status": "error",
                    "error": str(exc),
                }
            )
    return {"attempted": attempted, "count": len(attempted)}


def _require_admin(app_settings: Settings, provided_token: str | None) -> None:
    if app_settings.admin_token:
        if provided_token != app_settings.admin_token:
            raise HTTPException(status_code=401, detail="Invalid admin token")
        return
    if not app_settings.dry_run:
        raise HTTPException(
            status_code=403,
            detail="KONDO_FOLK_ADMIN_TOKEN is required for admin routes in live mode",
        )


def _action_response(
    request: Request,
    token: str | None,
    result: dict[str, Any],
    notice: str,
) -> dict[str, Any] | RedirectResponse:
    if token:
        return RedirectResponse(_console_url(token, notice), status_code=303)
    return result


def _console_url(token: str | None, notice: str | None = None) -> str:
    parts: list[str] = []
    if token:
        parts.append(f"token={quote(token)}")
    if notice:
        parts.append(f"notice={quote(notice)}")
    return "/console" + (f"?{'&'.join(parts)}" if parts else "")


def _console_html(app_settings: Settings, store: SyncStore, token: str | None, notice: str | None = None) -> str:
    events = store.recent_events(limit=50)
    triage = store.triage_events(limit=100)
    token_query = f"?token={html.escape(token)}" if token else ""
    summary = _review_summary(triage, store.queue_depth(app_settings.processing_timeout_seconds))
    summary_html = _summary_html(summary)
    batch_html = _batch_html(summary)
    triage_rows = "\n".join(_triage_row(item, token_query) for item in triage)
    if not triage_rows:
        triage_rows = "<tr><td colspan='10' class='muted'>No analyzed contacts yet.</td></tr>"
    rows = "\n".join(_event_row(event, token_query) for event in events)
    notice_html = (
        f"<section class='notice'>{html.escape(notice)}</section>"
        if notice
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kondo folk Sync Console</title>
  <style>
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #16211d;
      background: #f7f4ef;
    }}
    header {{
      padding: 24px 32px;
      background: #17372f;
      color: #fff;
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: center;
    }}
    h1 {{ font-size: 22px; margin: 0; letter-spacing: 0; }}
    h2 {{ font-size: 17px; margin: 0 0 10px; letter-spacing: 0; }}
    main {{ padding: 28px 32px; max-width: 1180px; margin: 0 auto; }}
    .status {{ font-size: 14px; opacity: .9; }}
    .metrics {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 24px; }}
    .metric {{
      background: #fff;
      border: 1px solid #ded8ce;
      border-radius: 8px;
      padding: 12px 16px;
      min-width: 130px;
      display: flex;
      justify-content: space-between;
      gap: 18px;
    }}
    .metric span {{ color: #59655f; }}
    .workflow {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 0 0 18px;
    }}
    .workflow-step {{
      background: #fff;
      border: 1px solid #ded8ce;
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .workflow-step strong {{
      display: block;
      font-size: 13px;
      margin-bottom: 3px;
    }}
    .batch-preview {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      background: #fff;
      border: 1px solid #ded8ce;
      border-radius: 8px;
      padding: 14px 16px;
      margin: 0 0 18px;
    }}
    .batch-preview h2 {{ margin: 0 0 4px; }}
    .notice {{
      margin: 0 0 18px;
      background: #e4efe7;
      border: 1px solid #b9d2bf;
      border-radius: 8px;
      padding: 11px 14px;
      color: #17372f;
      font-size: 14px;
    }}
    .panel {{ margin: 0 0 26px; }}
    .panel-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 10px;
    }}
    .hint {{ color: #64706a; font-size: 13px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border: 1px solid #ded8ce;
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid #ece6dc;
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}
    th {{ background: #ebe4d8; color: #39433e; font-weight: 650; }}
    code {{ font-size: 12px; }}
    .small {{ color: #64706a; font-size: 12px; margin-top: 3px; }}
    .muted {{ color: #64706a; }}
    .nowrap {{ white-space: nowrap; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 5px; max-width: 250px; }}
    .tag {{
      border: 1px solid #d8d0c3;
      background: #f7f4ef;
      border-radius: 999px;
      padding: 2px 7px;
      color: #3c4742;
      font-size: 12px;
      white-space: nowrap;
    }}
    .priority-score {{
      display: inline-flex;
      width: 28px;
      height: 28px;
      border-radius: 999px;
      align-items: center;
      justify-content: center;
      background: #dfeadf;
      color: #17372f;
      font-weight: 700;
    }}
    .actions {{ display: flex; gap: 10px; margin-bottom: 18px; }}
    .primary-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 0 0 18px;
      align-items: center;
    }}
    details {{
      margin: 20px 0 0;
      background: #fff;
      border: 1px solid #ded8ce;
      border-radius: 8px;
      padding: 12px 14px;
    }}
    summary {{ cursor: pointer; font-weight: 650; color: #39433e; }}
    details .actions {{ margin: 12px 0 0; }}
    .row-actions {{ display: flex; flex-wrap: wrap; gap: 7px; min-width: 180px; }}
    .select-cell {{ width: 34px; text-align: center; }}
    input[type="checkbox"] {{ width: 16px; height: 16px; }}
    button, .button-link {{
      border: 1px solid #17372f;
      background: #17372f;
      color: white;
      border-radius: 7px;
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .ghost {{ background: #fff; color: #17372f; }}
    .secondary {{ border-color: #bf7b2e; background: #bf7b2e; }}
    button:disabled {{
      border-color: #c8c0b5;
      background: #d8d0c3;
      color: #766f67;
      cursor: not-allowed;
    }}
    .error {{ color: #9b2c2c; max-width: 260px; }}
    .danger-zone {{
      margin-top: 18px;
      padding-top: 14px;
      border-top: 1px solid #ece6dc;
    }}
    .danger {{ border-color: #9b2c2c; background: #9b2c2c; }}
    .text-input {{
      border: 1px solid #d8d0c3;
      border-radius: 7px;
      padding: 8px 10px;
      font: inherit;
    }}
    @media (max-width: 880px) {{
      header {{ display: block; }}
      main {{ padding: 18px 14px; }}
      .workflow {{ grid-template-columns: 1fr; }}
      .batch-preview {{ display: block; }}
      table {{ display: block; overflow-x: auto; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Kondo folk Sync Console</h1>
    <div class="status">{_mode_label(app_settings)} · ai={html.escape(app_settings.ai_provider)}</div>
  </header>
  <main>
    {notice_html}
    <section class="metrics">{summary_html}</section>
    <section class="workflow">
      <div class="workflow-step"><strong>1. Review today</strong><span class="hint">Kondo activity appears here after it is analyzed.</span></div>
      <div class="workflow-step"><strong>2. Choose recap depth</strong><span class="hint">Send the latest recap, or open Kondo and request full history first.</span></div>
      <div class="workflow-step"><strong>3. Push selected</strong><span class="hint">folk is updated only from the selected batch.</span></div>
    </section>
    {batch_html}
    <section class="primary-actions">
      <a class="button-link ghost" href="/console{token_query}">Refresh</a>
      <form method="post" action="/admin/stage-all{token_query}">
        <button class="ghost" type="submit">Select All Unreviewed</button>
      </form>
      <form method="post" action="/admin/send-staged{token_query}">
        <button type="submit"{' disabled' if summary["selected"] == 0 else ''}>Send Selected Batch to folk</button>
      </form>
    </section>
    <section class="panel">
      <div class="panel-header">
        <h2>Daily Triage</h2>
        <div class="hint">Recent analyzed conversations, newest Kondo conversation first.</div>
      </div>
      <section class="actions">
        <form id="stage-selected-form" method="post" action="/admin/stage-selected{token_query}">
          <button class="ghost" id="select-checked-button" type="submit">Select Checked</button>
        </form>
        <div class="hint" id="checked-count">0 checked on this page.</div>
      </section>
      <table>
        <thead>
          <tr>
            <th class="select-cell">Select</th>
            <th>Score</th>
            <th>Contact</th>
            <th>Send Status</th>
            <th>Last Message</th>
            <th>Bucket</th>
            <th>Conversation</th>
            <th>Next Action</th>
            <th>Why</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>{triage_rows}</tbody>
      </table>
    </section>
    <details>
      <summary>Advanced queue tools</summary>
      <section class="actions">
        <form method="post" action="/admin/process{token_query}">
          <button class="ghost" type="submit">Process Incoming Queue</button>
        </form>
        <form method="post" action="/admin/reconcile{token_query}">
          <button class="ghost" type="submit">Retry Failed/Due Work</button>
        </form>
        <a class="button-link ghost" href="/admin/stats{token_query}">JSON Stats</a>
      </section>
      <table>
        <thead>
          <tr>
            <th>Status</th>
            <th>LinkedIn</th>
            <th>Updated</th>
            <th>Error</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <section class="danger-zone">
        <h2>Reset Local Sync State</h2>
        <div class="hint">Use only after a folk hard reset. This clears received Kondo events and stored folk ID mappings in this service.</div>
        <form class="primary-actions" method="post" action="/admin/reset-local-state{token_query}">
          <input class="text-input" name="confirm" placeholder="Type RESET">
          <button class="danger" type="submit">Clear Local Sync State</button>
        </form>
      </section>
    </details>
  </main>
</body>
<script>
  const checkedCount = document.getElementById("checked-count");
  const selectButton = document.getElementById("select-checked-button");
  const checkboxes = Array.from(document.querySelectorAll("input[name='selected']"));
  function updateCheckedCount() {{
    const checked = checkboxes.filter((box) => box.checked).length;
    checkedCount.textContent = `${{checked}} checked on this page.`;
    selectButton.textContent = checked === 0 ? "Select Checked" : `Select ${{checked}} Checked`;
    selectButton.disabled = checked === 0;
  }}
  checkboxes.forEach((box) => box.addEventListener("change", updateCheckedCount));
  updateCheckedCount();
</script>
</html>"""


def _review_summary(items: list[dict[str, Any]], queue_depth: int) -> dict[str, int]:
    summary = {
        "needs_review": 0,
        "selected": 0,
        "selected_latest": 0,
        "selected_full": 0,
        "waiting_full_history": 0,
        "sent": 0,
        "failed": 0,
        "queue_depth": queue_depth,
    }
    for item in items:
        status = str(item.get("status") or "")
        sync_depth = str(item.get("sync_depth") or "")
        if status == "review_pending":
            summary["needs_review"] += 1
        elif status == "full_sync_requested":
            summary["waiting_full_history"] += 1
        elif status == "staged_for_folk":
            summary["selected"] += 1
            if sync_depth == "full_history":
                summary["selected_full"] += 1
            else:
                summary["selected_latest"] += 1
        elif status in {"synced", "dry_run"}:
            summary["sent"] += 1
        elif status == "error":
            summary["failed"] += 1
    return summary


def _summary_html(summary: dict[str, int]) -> str:
    cards = (
        ("Needs review", summary["needs_review"]),
        ("Selected to send", summary["selected"]),
        ("Waiting for full history", summary["waiting_full_history"]),
        ("Sent to folk", summary["sent"]),
        ("Queue", summary["queue_depth"]),
    )
    return "".join(
        f"<div class='metric'><span>{html.escape(label)}</span><strong>{count}</strong></div>"
        for label, count in cards
    )


def _batch_html(summary: dict[str, int]) -> str:
    if summary["selected"] == 0:
        body = "No contacts are in the selected batch yet."
    else:
        body = (
            f"{summary['selected']} selected: "
            f"{summary['selected_latest']} latest-message recap(s), "
            f"{summary['selected_full']} full-history recap(s)."
        )
    return f"""<section class="batch-preview">
  <div>
    <h2>Selected Batch</h2>
    <div class="hint">{html.escape(body)}</div>
  </div>
  <div class="hint">Nothing is written to folk until you click Send Selected Batch to folk.</div>
</section>"""


def _mode_label(app_settings: Settings) -> str:
    write_mode = "Live folk writes" if not app_settings.dry_run else "Dry-run preview"
    review_mode = "review gate on" if app_settings.review_mode else "auto-send mode"
    worker_mode = "worker on" if app_settings.worker_enabled else "worker off"
    return f"{write_mode} · {review_mode} · {worker_mode}"


def _decision_label(status: str) -> str:
    labels = {
        "review_pending": "Not selected",
        "full_sync_requested": "Waiting for full history",
        "staged_for_folk": "Selected for next send",
        "queued_for_folk": "Sending to folk",
        "synced": "Sent to folk",
        "dry_run": "Dry-run only",
        "excluded": "Skipped",
        "error": "Needs retry",
    }
    return labels.get(status, status.replace("_", " "))


def _triage_row(item: dict[str, Any], token_query: str) -> str:
    key = html.escape(str(item["idempotency_key"]))
    linkedin_url = html.escape(str(item.get("linkedin_url") or ""))
    kondo_url = html.escape(str(item.get("kondo_url") or ""))
    status = str(item.get("status") or "")
    decision_label = _decision_label(status)
    sync_depth = str(item.get("sync_depth") or "latest_message")
    depth_label = "Full history" if sync_depth == "full_history" else "Latest message"
    stage_label = "Select Full History" if sync_depth == "full_history" else "Select Latest Recap"
    can_stage = status not in {"excluded", "queued_for_folk"}
    checkbox = (
        f"<input form='stage-selected-form' type='checkbox' name='selected' value='{key}'>"
        if can_stage
        else ""
    )
    actions: list[str] = []
    if status == "review_pending":
        actions.append(
            f"<form method='post' action='/admin/stage/{key}{token_query}'>"
            f"<button type='submit'>{stage_label}</button></form>"
        )
        actions.append(
            f"<form method='post' action='/admin/request-full-sync/{key}{token_query}'>"
            "<button class='ghost secondary' type='submit'>Get Full History First</button></form>"
        )
    elif status == "full_sync_requested" and kondo_url:
        actions.append(f"<a class='button-link secondary' href='{kondo_url}' target='_blank' rel='noreferrer'>Open Kondo Full Sync</a>")
        actions.append(
            f"<form method='post' action='/admin/stage/{key}{token_query}'>"
            f"<button class='ghost' type='submit'>{stage_label}</button></form>"
        )
    elif status == "staged_for_folk":
        actions.append("<span class='tag'>in selected batch</span>")
        actions.append(
            f"<form method='post' action='/admin/unstage/{key}{token_query}'>"
            "<button class='ghost' type='submit'>Remove from Batch</button></form>"
        )
    elif status == "queued_for_folk":
        actions.append("<span class='tag'>queued for folk</span>")
    elif status in {"synced", "dry_run"}:
        actions.append("<span class='tag'>already sent before</span>")
        actions.append(
            f"<form method='post' action='/admin/stage/{key}{token_query}'>"
            f"<button class='ghost' type='submit'>Select to Resend {depth_label}</button></form>"
        )
    if linkedin_url:
        actions.append(f"<a class='button-link ghost' href='{linkedin_url}' target='_blank' rel='noreferrer'>LinkedIn</a>")
    if not actions:
        actions.append("<span class='muted'>No action</span>")
    reasons = "".join(f"<span class='tag'>{html.escape(str(reason))}</span>" for reason in item.get("reasons", []))
    if not reasons:
        reasons = "<span class='muted'>review</span>"
    full_history = "Full-history refresh recommended" if item.get("needs_full_history") else "Latest-message sync is probably enough"
    follow_up = item.get("follow_up_date")
    next_action = html.escape(str(item.get("next_action") or "Review conversation."))
    if follow_up:
        next_action = f"{next_action}<div class='small'>Follow up: {html.escape(str(follow_up))}</div>"
    return f"""<tr>
  <td class='select-cell'>{checkbox}</td>
  <td><span class="priority-score">{html.escape(str(item.get("score") or 0))}</span></td>
  <td>
    {html.escape(str(item.get("full_name") or "Unknown contact"))}
    <div class='small'>{full_history}</div>
  </td>
  <td>
    {html.escape(decision_label)}
    <div class='small'><span class='tag'>{depth_label}</span></div>
  </td>
  <td>{html.escape(str(item.get("conversation_time") or ""))}</td>
  <td>{html.escape(str(item.get("group_category") or ""))}</td>
  <td>
    {html.escape(str(item.get("relationship_stage") or ""))}
    <div class='small'>{html.escape(str(item.get("reply_owner") or ""))} · confidence {html.escape(str(item.get("confidence") or 0))}</div>
  </td>
  <td>{next_action}</td>
  <td><div class='tags'>{reasons}</div></td>
  <td><div class='row-actions'>{" ".join(actions)}</div></td>
</tr>"""


def _event_row(event: dict[str, Any], token_query: str) -> str:
    key = html.escape(str(event["idempotency_key"]))
    linkedin_url = html.escape(str(event.get("linkedin_url") or ""))
    linkedin = f"<a href='{linkedin_url}'>{linkedin_url}</a>" if linkedin_url else ""
    error = html.escape(str(event.get("error") or ""))
    return f"""<tr>
  <td>{html.escape(_decision_label(str(event.get("status") or "")))}</td>
  <td>{linkedin}</td>
  <td>{html.escape(str(event.get("updated_at") or ""))}</td>
  <td class="error">{error}</td>
  <td>
    <form method="post" action="/admin/reprocess/{key}{token_query}">
      <button class="ghost" type="submit">Reprocess</button>
    </form>
  </td>
</tr>"""


app = create_app()
