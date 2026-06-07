from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import quote
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .ai import AIAnalyzer
from .config import Settings, TeamUser, settings
from .folk import FolkClient, FolkRateLimitError
from .models import AIAnalysis, normalize_kondo_payload
from .store import SyncStore


STATIC_DIR = Path(__file__).parent / "static"
CONSOLE_TEMPLATE = STATIC_DIR / "console" / "index.html"


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
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
        return HTMLResponse(_console_html(app_settings, store, token, notice, user=_default_user(app_settings)))

    @app.get("/console/{user_slug}", response_class=HTMLResponse)
    async def user_console(
        user_slug: str,
        request: Request,
        token: str | None = None,
        notice: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> HTMLResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        return HTMLResponse(_console_html(app_settings, store, token, notice, user=user))

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

    @app.get("/admin/{user_slug}/stats")
    async def user_admin_stats(
        user_slug: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        return {
            "user": {"slug": user.slug, "name": user.name},
            "health": {
                "ok": True,
                "dry_run": app_settings.dry_run,
                "ai_provider": app_settings.ai_provider,
                "database": str(app_settings.database_path),
                "reconcile_interval_minutes": app_settings.reconcile_interval_minutes,
                "worker_enabled": app_settings.worker_enabled,
                "review_mode": app_settings.review_mode,
                "queue_depth": store.queue_depth(
                    app_settings.processing_timeout_seconds,
                    user_slug=user.slug,
                ),
                "processing_timeout_seconds": app_settings.processing_timeout_seconds,
            },
            "counts": store.status_counts(user_slug=user.slug),
            "recent_events": store.recent_events(limit=25, user_slug=user.slug),
            "retryable_events": store.retryable_events(
                limit=25,
                processing_timeout_seconds=app_settings.processing_timeout_seconds,
                user_slug=user.slug,
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

    @app.get("/admin/console-state")
    async def admin_console_state(
        limit: int = 250,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_admin(app_settings, token or x_admin_token)
        return _console_state(
            app_settings,
            store,
            limit=max(25, min(limit, 500)),
        )

    @app.get("/admin/{user_slug}/console-state")
    async def user_admin_console_state(
        user_slug: str,
        limit: int = 250,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        return _console_state(
            app_settings,
            store,
            limit=max(25, min(limit, 500)),
            user=user,
        )

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

    @app.post("/admin/skip/{idempotency_key}", response_model=None)
    async def admin_skip(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.skip_event(idempotency_key)
        result = {"status": "excluded", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Skipped one contact.")

    @app.post("/admin/mark-relevant/{idempotency_key}", response_model=None)
    async def admin_mark_relevant(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        form = await request.form()
        group_category = str(form.get("group_category") or "")
        store.mark_relevant(idempotency_key, group_category=group_category)
        result = {"status": "review_pending", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Marked one contact relevant.")

    @app.post("/admin/group/{idempotency_key}", response_model=None)
    async def admin_group(
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        _require_admin(app_settings, token or x_admin_token)
        if store.get_event(idempotency_key) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        form = await request.form()
        group_category = str(form.get("group_category") or "")
        store.update_group_category(idempotency_key, group_category)
        result = {
            "status": "group_updated",
            "idempotency_key": idempotency_key,
            "group_category": group_category,
        }
        return _action_response(request, token, result, "Updated one bucket.")

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

    @app.post("/admin/{user_slug}/reprocess/{idempotency_key}", response_model=None)
    async def user_admin_reprocess(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        payload = store.get_event_payload(idempotency_key, user_slug=user.slug)
        if payload is None:
            raise HTTPException(status_code=404, detail="Event not found")
        result = _enqueue_payload(payload, store, force=True, user_slug=user.slug, scope_key=True)
        return _action_response(request, token, result, "Requeued one stored payload.", user=user)

    @app.post("/admin/{user_slug}/stage/{idempotency_key}", response_model=None)
    async def user_admin_stage(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.stage_for_folk(idempotency_key, user_slug=user.slug)
        result = {"status": "staged_for_folk", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Staged one row for the batch.", user=user)

    @app.post("/admin/{user_slug}/stage-all", response_model=None)
    async def user_admin_stage_all(
        user_slug: str,
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        count = store.stage_all_for_folk(user_slug=user.slug)
        result = {"status": "staged_for_folk", "count": count}
        return _action_response(request, token, result, f"Staged {count} pending row(s).", user=user)

    @app.post("/admin/{user_slug}/send-staged", response_model=None)
    async def user_admin_send_staged(
        user_slug: str,
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        count = store.queue_staged_for_folk(user_slug=user.slug)
        result = {"status": "queued_for_folk", "count": count}
        return _action_response(request, token, result, f"Queued {count} staged row(s) for folk.", user=user)

    @app.post("/admin/{user_slug}/unstage/{idempotency_key}", response_model=None)
    async def user_admin_unstage(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.unstage_for_folk(idempotency_key, user_slug=user.slug)
        result = {"status": "unstaged", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Removed one row from the send batch.", user=user)

    @app.post("/admin/{user_slug}/skip/{idempotency_key}", response_model=None)
    async def user_admin_skip(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.skip_event(idempotency_key, user_slug=user.slug)
        result = {"status": "excluded", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Skipped one contact.", user=user)

    @app.post("/admin/{user_slug}/mark-relevant/{idempotency_key}", response_model=None)
    async def user_admin_mark_relevant(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        form = await request.form()
        store.mark_relevant(idempotency_key, group_category=str(form.get("group_category") or ""), user_slug=user.slug)
        result = {"status": "review_pending", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Marked one contact relevant.", user=user)

    @app.post("/admin/{user_slug}/group/{idempotency_key}", response_model=None)
    async def user_admin_group(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        form = await request.form()
        group_category = str(form.get("group_category") or "")
        store.update_group_category(idempotency_key, group_category, user_slug=user.slug)
        result = {"status": "group_updated", "idempotency_key": idempotency_key, "group_category": group_category}
        return _action_response(request, token, result, "Updated one bucket.", user=user)

    @app.post("/admin/{user_slug}/request-full-sync/{idempotency_key}", response_model=None)
    async def user_admin_request_full_sync(
        user_slug: str,
        request: Request,
        idempotency_key: str,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        if store.get_event(idempotency_key, user_slug=user.slug) is None:
            raise HTTPException(status_code=404, detail="Event not found")
        store.request_full_sync(idempotency_key, user_slug=user.slug)
        result = {"status": "full_sync_requested", "idempotency_key": idempotency_key}
        return _action_response(request, token, result, "Marked one row for full-history sync.", user=user)

    @app.post("/admin/{user_slug}/reset-local-state", response_model=None)
    async def user_admin_reset_local_state(
        user_slug: str,
        request: Request,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        form = await request.form()
        if str(form.get("confirm") or "") != "RESET":
            raise HTTPException(status_code=400, detail="Type RESET to clear local sync state")
        store.reset_user(user.slug)
        result = {"status": "reset", "message": f"local sync state cleared for {user.slug}"}
        return _action_response(request, token, result, "Cleared local sync state.", user=user)

    @app.post("/admin/{user_slug}/reconcile", response_model=None)
    async def user_admin_reconcile(
        user_slug: str,
        request: Request,
        limit: int = 25,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        result = await _process_queue(
            store,
            analyzer,
            folk,
            limit=min(limit, 100),
            processing_timeout_seconds=app_settings.processing_timeout_seconds,
            user_slug=user.slug,
        )
        return _action_response(request, token, result, f"Retried {result['count']} queued/retry item(s).", user=user)

    @app.post("/admin/{user_slug}/process", response_model=None)
    async def user_admin_process(
        user_slug: str,
        request: Request,
        limit: int = 25,
        token: str | None = None,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any] | RedirectResponse:
        user = _resolve_user(app_settings, user_slug)
        _require_user_admin(app_settings, user, token or x_admin_token)
        result = await _process_queue(
            store,
            analyzer,
            folk,
            limit=min(limit, 100),
            processing_timeout_seconds=app_settings.processing_timeout_seconds,
            user_slug=user.slug,
        )
        return _action_response(request, token, result, f"Processed {result['count']} queued item(s).", user=user)

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

    @app.post("/webhooks/kondo/{user_slug}")
    async def user_kondo_webhook(
        user_slug: str,
        request: Request,
        x_kondo_webhook_secret: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        user = _resolve_user(app_settings, user_slug)
        provided_secret = x_kondo_webhook_secret or x_api_key
        if user.webhook_secret and provided_secret != user.webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Expected JSON object")
        return _enqueue_payload(payload, store, user_slug=user.slug, scope_key=True)

    @app.post("/sync/manual")
    async def manual_sync(payload: dict[str, Any]) -> dict[str, Any]:
        return _enqueue_payload(payload, store)

    @app.post("/sync/manual/{user_slug}")
    async def user_manual_sync(user_slug: str, payload: dict[str, Any]) -> dict[str, Any]:
        user = _resolve_user(app_settings, user_slug)
        return _enqueue_payload(payload, store, user_slug=user.slug, scope_key=True)

    return app


def _enqueue_payload(
    payload: dict[str, Any],
    store: SyncStore,
    force: bool = False,
    user_slug: str = "default",
    scope_key: bool = False,
) -> dict[str, Any]:
    event = normalize_kondo_payload(payload)
    event_key = _event_key(event.idempotency_key, user_slug, scope_key)
    existing = store.get_event(event_key, user_slug=user_slug)
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
            "idempotency_key": event_key,
            "previous_status": existing["status"],
        }
    queued = store.queue_event(
        event_key,
        event.linkedin_url,
        event.to_dict(),
        force=force,
        user_slug=user_slug,
    )
    return {
        "status": queued["status"],
        "idempotency_key": event_key,
        "raw_idempotency_key": event.idempotency_key,
        "user_slug": user_slug,
        "linkedin_url": event.linkedin_url,
    }


async def _process_payload(
    payload: dict[str, Any],
    store: SyncStore,
    analyzer: AIAnalyzer,
    folk: FolkClient,
    force: bool = False,
    bypass_review: bool = False,
    idempotency_key: str | None = None,
    user_slug: str = "default",
) -> dict[str, Any]:
    event = normalize_kondo_payload(payload)
    event_key = idempotency_key or event.idempotency_key
    existing = store.get_event(event_key, user_slug=user_slug)
    if existing and force:
        store.delete_event(event_key, user_slug=user_slug)
        existing = None
    if (
        existing
        and not bypass_review
        and _should_skip_existing(existing["status"], event.linkedin_url, folk.settings.dry_run)
    ):
        return {
            "status": "duplicate",
            "idempotency_key": event_key,
            "previous_status": existing["status"],
        }

    store.start_event(event_key, event.linkedin_url, event.to_dict(), user_slug=user_slug)
    try:
        if not event.linkedin_url:
            result = {"status": "held_for_review", "reason": "missing_linkedin_url"}
            store.finish_event(event_key, "held_for_review", result=result, user_slug=user_slug)
            return {"idempotency_key": event_key, **result}

        stored_analysis = store.get_event_analysis(event_key, user_slug=user_slug)
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
                event_key,
                "excluded",
                analysis=analysis.to_dict(),
                result=result,
                user_slug=user_slug,
            )
            return {
                "idempotency_key": event_key,
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
                event_key,
                "review_pending",
                analysis=analysis.to_dict(),
                result=result,
                user_slug=user_slug,
            )
            upgraded_to_selected = False
            if event.has_full_history:
                upgraded_to_selected = store.auto_stage_full_history_if_latest_selected(
                    event.linkedin_url,
                    event_key,
                    user_slug=user_slug,
                )
                if upgraded_to_selected:
                    result["status"] = "staged_for_folk"
                    result["reason"] = "full_history_auto_upgraded_selected_batch"
            return {
                "idempotency_key": event_key,
                "analysis": analysis.to_dict(),
                "result": result,
            }
        write_analysis = _analysis_with_owner(analysis, user_slug)
        result = await folk.sync(event, write_analysis)
        status = str(result.get("status") or "synced")
        store.finish_event(
            event_key,
            status,
            analysis=write_analysis.to_dict(),
            result=result,
            user_slug=user_slug,
        )
        return {
            "idempotency_key": event_key,
            "analysis": write_analysis.to_dict(),
            "result": result,
        }
    except FolkRateLimitError as exc:
        store.defer_event(event_key, str(exc), exc.retry_at, user_slug=user_slug)
        return {
            "idempotency_key": event_key,
            "result": {
                "status": "retry_wait",
                "retry_at": exc.retry_at.astimezone(UTC).isoformat(),
                "reason": str(exc),
            },
        }
    except Exception as exc:
        store.finish_event(event_key, "error", error=str(exc), user_slug=user_slug)
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


def _analysis_with_owner(analysis: AIAnalysis, user_slug: str) -> AIAnalysis:
    if user_slug == "default":
        return analysis
    data = analysis.to_dict()
    owner_line = f"LinkedIn owner: {user_slug}"
    crm_note = str(data.get("crm_note") or "")
    if owner_line not in crm_note:
        data["crm_note"] = f"{owner_line}\n\n{crm_note}".strip()
    context = list(data.get("important_context") or [])
    if owner_line not in context:
        context.append(owner_line)
    data["important_context"] = context
    return AIAnalysis.from_dict(data)


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
    user_slug: str | None = None,
) -> dict[str, Any]:
    attempted: list[dict[str, Any]] = []
    for _ in range(limit):
        event = store.next_queued_event(
            processing_timeout_seconds=processing_timeout_seconds,
            user_slug=user_slug,
        )
        if event is None:
            break
        idempotency_key = str(event["idempotency_key"])
        event_user_slug = str(event.get("user_slug") or "default")
        payload = store.get_event_payload(idempotency_key, user_slug=event_user_slug)
        if payload is None:
            attempted.append(
                {
                    "idempotency_key": idempotency_key,
                    "status": "missing_payload",
                }
            )
            continue
        store.mark_processing(idempotency_key, user_slug=event_user_slug)
        try:
            result = await _process_payload(
                payload,
                store,
                analyzer,
                folk,
                bypass_review=str(event.get("status") or "") == "queued_for_folk",
                idempotency_key=idempotency_key,
                user_slug=event_user_slug,
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


def _default_user(app_settings: Settings) -> TeamUser:
    if app_settings.team_users:
        return app_settings.team_users[0]
    return TeamUser(
        slug="default",
        name="Default",
        admin_token=app_settings.admin_token,
        webhook_secret=app_settings.webhook_secret,
    )


def _resolve_user(app_settings: Settings, user_slug: str) -> TeamUser:
    normalized = user_slug.strip().lower()
    for user in app_settings.team_users:
        if user.slug == normalized:
            return user
    if not app_settings.team_users and normalized == "default":
        return _default_user(app_settings)
    raise HTTPException(status_code=404, detail="Unknown sync user")


def _require_user_admin(
    app_settings: Settings,
    user: TeamUser,
    provided_token: str | None,
) -> None:
    expected_token = user.admin_token or app_settings.admin_token
    if expected_token:
        if provided_token != expected_token:
            raise HTTPException(status_code=401, detail="Invalid admin token")
        return
    if not app_settings.dry_run:
        raise HTTPException(
            status_code=403,
            detail="An admin token is required for user consoles in live mode",
        )


def _event_key(raw_idempotency_key: str, user_slug: str, scope_key: bool) -> str:
    if not scope_key or user_slug == "default":
        return raw_idempotency_key
    prefix = f"{user_slug}:"
    if raw_idempotency_key.startswith(prefix):
        return raw_idempotency_key
    return f"{prefix}{raw_idempotency_key}"


def _action_response(
    request: Request,
    token: str | None,
    result: dict[str, Any],
    notice: str,
    user: TeamUser | None = None,
) -> dict[str, Any] | RedirectResponse:
    if token:
        return RedirectResponse(_console_url(token, notice, user=user), status_code=303)
    return result


def _console_url(token: str | None, notice: str | None = None, user: TeamUser | None = None) -> str:
    parts: list[str] = []
    if token:
        parts.append(f"token={quote(token)}")
    if notice:
        parts.append(f"notice={quote(notice)}")
    path = f"/console/{quote(user.slug)}" if user and user.slug != "default" else "/console"
    return path + (f"?{'&'.join(parts)}" if parts else "")


def _mode_label(app_settings: Settings) -> str:
    if app_settings.dry_run:
        return "Dry run"
    if app_settings.review_mode:
        return "Live review"
    return "Live direct"


def _console_state(
    app_settings: Settings,
    store: SyncStore,
    limit: int = 250,
    user: TeamUser | None = None,
) -> dict[str, Any]:
    active_user = user or _default_user(app_settings)
    rows = [_console_row(item) for item in store.triage_events(limit=limit, user_slug=active_user.slug)]
    rows = _dedupe_console_rows(rows)
    summary = {
        "needs_review": sum(1 for row in rows if row["console_state"] in {"review", "full_ready"}),
        "selected": sum(1 for row in rows if row["console_state"] == "selected"),
        "selected_latest": sum(
            1 for row in rows if row["console_state"] == "selected" and row["sync_depth"] != "full_history"
        ),
        "selected_full": sum(
            1 for row in rows if row["console_state"] == "selected" and row["sync_depth"] == "full_history"
        ),
        "waiting": sum(1 for row in rows if row["console_state"] == "waiting"),
        "sent": sum(1 for row in rows if row["console_state"] == "sent"),
        "skipped": sum(1 for row in rows if row["console_state"] == "skipped"),
        "queue_depth": store.queue_depth(app_settings.processing_timeout_seconds, user_slug=active_user.slug),
    }
    status_counts = store.status_counts(user_slug=active_user.slug)
    last_event_at = max((str(row.get("updated_at") or "") for row in rows), default="")
    revision = "|".join(
        [
            last_event_at,
            str(summary["queue_depth"]),
            str(summary["needs_review"]),
            str(summary["selected"]),
            str(summary["waiting"]),
            json.dumps(status_counts, sort_keys=True),
        ]
    )
    return {
        "mode": _mode_label(app_settings),
        "ai_provider": app_settings.ai_provider,
        "user": {"slug": active_user.slug, "name": active_user.name},
        "summary": summary,
        "status_counts": status_counts,
        "rows": rows,
        "last_event_at": last_event_at,
        "revision": revision,
    }


def _console_row(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "")
    sync_depth = str(item.get("sync_depth") or "latest_message")
    has_full_history = sync_depth == "full_history"
    if status == "staged_for_folk":
        console_state = "selected"
        console_label = "Selected: full conversation" if has_full_history else "Selected: latest message"
    elif status == "full_sync_requested":
        console_state = "waiting"
        console_label = "Waiting for full history"
    elif status in {"synced", "dry_run", "queued_for_folk"}:
        console_state = "sent"
        console_label = "Sent to folk" if status == "synced" else "Queued/sent"
    elif status == "excluded":
        console_state = "skipped"
        console_label = "Skipped"
    elif has_full_history:
        console_state = "full_ready"
        console_label = "Full history ready"
    else:
        console_state = "review"
        console_label = "Needs review"
    row = dict(item)
    row.update(
        {
            "console_state": console_state,
            "console_label": console_label,
        }
    )
    return row


def _dedupe_console_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        contact_key = str(row.get("linkedin_url") or row.get("idempotency_key") or "")
        existing = grouped.get(contact_key)
        if existing is None or _row_rank(row) > _row_rank(existing):
            grouped[contact_key] = row
    deduped = list(grouped.values())
    deduped.sort(
        key=lambda row: (
            _state_sort_rank(str(row.get("console_state") or "")),
            str(row.get("conversation_time") or ""),
            str(row.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return deduped


def _row_rank(row: dict[str, Any]) -> tuple[int, int, str, str]:
    state = str(row.get("console_state") or "")
    depth = 2 if str(row.get("sync_depth") or "") == "full_history" else 0
    state_priority = {
        "selected": 10,
        "full_ready": 8,
        "waiting": 7,
        "review": 6,
        "sent": 4,
        "skipped": 2,
    }.get(state, 0)
    return (
        state_priority + depth,
        int(row.get("score") or 0),
        str(row.get("conversation_time") or ""),
        str(row.get("updated_at") or ""),
    )


def _state_sort_rank(state: str) -> int:
    return {
        "full_ready": 6,
        "review": 5,
        "waiting": 4,
        "selected": 3,
        "sent": 2,
        "skipped": 1,
    }.get(state, 0)


def _console_html(
    app_settings: Settings,
    store: SyncStore,
    token: str | None,
    notice: str | None = None,
    user: TeamUser | None = None,
) -> str:
    active_user = user or _default_user(app_settings)
    is_scoped = active_user.slug != "default" or bool(app_settings.team_users)
    admin_prefix = f"/admin/{active_user.slug}" if is_scoped else "/admin"
    stats_href = (
        f"/admin/{active_user.slug}/stats" if is_scoped else "/admin/stats"
    ) + (f"?token={quote(token)}" if token else "")
    config = {
        "token": token or "",
        "notice": notice or "",
        "adminPrefix": admin_prefix,
        "user": {"slug": active_user.slug, "name": active_user.name},
        "mode": _mode_label(app_settings),
        "aiProvider": app_settings.ai_provider,
    }
    return (
        CONSOLE_TEMPLATE.read_text(encoding="utf-8")
        .replace("__USER_NAME__", html.escape(active_user.name))
        .replace("__MODE__", html.escape(_mode_label(app_settings)))
        .replace("__AI__", html.escape(app_settings.ai_provider))
        .replace("__STATS_HREF__", html.escape(stats_href))
        .replace("__CONSOLE_CONFIG_JSON__", json.dumps(config))
    )


app = create_app()
