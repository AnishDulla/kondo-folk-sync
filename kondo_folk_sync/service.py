from __future__ import annotations

import asyncio
import html
import json
from urllib.parse import quote
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .ai import AIAnalyzer
from .config import Settings, TeamUser, settings
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


def _console_html(app_settings: Settings, store: SyncStore, token: str | None, notice: str | None = None) -> str:
    events = store.recent_events(limit=50)
    triage = store.triage_events(limit=100)
    token_query = f"?token={html.escape(token)}" if token else ""
    summary = _review_summary(triage, store.queue_depth(app_settings.processing_timeout_seconds))
    summary_html = _summary_html(summary)
    batch_html = _batch_html(summary)
    triage_rows = "\n".join(_triage_row(item, token_query) for item in triage)
    if not triage_rows:
        triage_rows = "<div class='empty-state'>No analyzed contacts yet.</div>"
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
      color: #17201b;
      background: #f5f7f8;
    }}
    header {{
      padding: 18px 32px;
      background: #fff;
      color: #17201b;
      display: flex;
      justify-content: space-between;
      gap: 24px;
      align-items: center;
      border-bottom: 1px solid #e2e7e4;
      position: sticky;
      top: 0;
      z-index: 10;
    }}
    h1 {{ font-size: 20px; margin: 0; letter-spacing: 0; }}
    h2 {{ font-size: 17px; margin: 0 0 8px; letter-spacing: 0; }}
    main {{ padding: 26px 32px 40px; max-width: 1280px; margin: 0 auto; }}
    .status {{
      font-size: 13px;
      color: #52605a;
      background: #f4f7f5;
      border: 1px solid #dfe6e2;
      border-radius: 999px;
      padding: 6px 10px;
    }}
    .metrics {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 24px; }}
    .metric {{
      background: #fff;
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      padding: 13px 16px;
      min-width: 150px;
      display: flex;
      justify-content: space-between;
      gap: 18px;
      box-shadow: 0 1px 2px rgba(17, 24, 39, .04);
    }}
    .metric span {{ color: #52605a; }}
    .metric strong {{ font-size: 18px; }}
    .workflow {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
      margin: 0 0 16px;
    }}
    .workflow-step {{
      background: #fff;
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      padding: 14px 16px;
      box-shadow: 0 1px 2px rgba(17, 24, 39, .04);
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
      background: #10251f;
      color: #fff;
      border: 1px solid #10251f;
      border-radius: 8px;
      padding: 16px 18px;
      margin: 0 0 16px;
      box-shadow: 0 10px 28px rgba(16, 37, 31, .14);
    }}
    .batch-preview h2 {{ margin: 0 0 4px; }}
    .batch-preview .hint {{ color: #d9e7df; }}
    .notice {{
      margin: 0 0 18px;
      background: #e8f5ed;
      border: 1px solid #b7dcc5;
      border-radius: 8px;
      padding: 11px 14px;
      color: #143b2a;
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
    .hint {{ color: #5e6b65; font-size: 13px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid #edf1ef;
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }}
    th {{ background: #f2f5f3; color: #39433e; font-weight: 650; }}
    code {{ font-size: 12px; }}
    .small {{ color: #5e6b65; font-size: 12px; margin-top: 3px; }}
    .muted {{ color: #5e6b65; }}
    .nowrap {{ white-space: nowrap; }}
    .tags {{ display: flex; flex-wrap: wrap; gap: 5px; max-width: 250px; }}
    .tag {{
      border: 1px solid #dde5e0;
      background: #f7faf8;
      border-radius: 999px;
      padding: 3px 8px;
      color: #3d4943;
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
      background: #e8f5ed;
      color: #16442e;
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
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      padding: 12px 14px;
    }}
    summary {{ cursor: pointer; font-weight: 650; color: #39433e; }}
    details .actions {{ margin: 12px 0 0; }}
    .triage-list {{
      display: grid;
      gap: 12px;
    }}
    .contact-card {{
      background: #fff;
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 2px rgba(17, 24, 39, .04);
      transition: border-color .12s ease, box-shadow .12s ease;
    }}
    .contact-card:hover {{ box-shadow: 0 8px 24px rgba(17, 24, 39, .07); }}
    .contact-card.selected {{ border-color: #87b898; box-shadow: inset 4px 0 0 #238451, 0 8px 24px rgba(17, 24, 39, .05); }}
    .contact-card.full-ready {{ box-shadow: inset 4px 0 0 #238451, 0 8px 24px rgba(17, 24, 39, .05); }}
    .contact-card.waiting {{ box-shadow: inset 4px 0 0 #c5791d, 0 8px 24px rgba(17, 24, 39, .05); }}
    .contact-card.skipped {{ opacity: .72; }}
    .card-top {{
      display: grid;
      grid-template-columns: 34px minmax(220px, 1fr) auto;
      gap: 12px;
      align-items: start;
      padding: 14px 16px;
      border-bottom: 1px solid #edf1ef;
      background: #fbfcfb;
    }}
    .contact-name {{ font-weight: 760; margin-bottom: 6px; font-size: 15px; }}
    .contact-meta {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .status-stack {{ display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; max-width: 430px; }}
    .status-pill {{
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      font-weight: 650;
      background: #eaf6ef;
      color: #17452f;
      border: 1px solid #bee1c9;
      white-space: nowrap;
    }}
    .status-pill.warn {{ background: #fff2df; border-color: #edc27e; color: #78450f; }}
    .status-pill.neutral {{ background: #f5f7f6; border-color: #dfe6e2; color: #4d5752; }}
    .status-pill.done {{ background: #e4f5ea; border-color: #a9dcb9; color: #17452f; }}
    .card-body {{
      display: grid;
      grid-template-columns: minmax(240px, 1.1fr) minmax(220px, 1fr) minmax(180px, .7fr);
      gap: 18px;
      padding: 14px 16px 16px;
    }}
    .card-section-title {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: #68736d;
      font-weight: 750;
      margin-bottom: 6px;
    }}
    .next-action {{ font-size: 13px; line-height: 1.35; }}
    .action-rail {{ display: flex; flex-wrap: wrap; gap: 8px; align-content: flex-start; justify-content: flex-end; }}
    .empty-state {{
      background: #fff;
      border: 1px solid #e1e7e3;
      border-radius: 8px;
      padding: 20px;
      color: #5e6b65;
    }}
    .row-actions {{ display: flex; flex-wrap: wrap; gap: 7px; min-width: 180px; }}
    .select-cell {{ width: 34px; text-align: center; }}
    input[type="checkbox"] {{ width: 16px; height: 16px; }}
    button, .button-link {{
      border: 1px solid #10251f;
      background: #10251f;
      color: white;
      border-radius: 7px;
      padding: 9px 12px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    button:hover, .button-link:hover {{ filter: brightness(.97); }}
    .ghost {{ background: #fff; color: #10251f; }}
    .secondary {{ border-color: #c5791d; background: #c5791d; }}
    button:disabled {{
      border-color: #d8dfdb;
      background: #e4e9e6;
      color: #75817b;
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
      .card-top, .card-body {{ grid-template-columns: 1fr; }}
      .status-stack, .action-rail {{ justify-content: flex-start; }}
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
      <div class="triage-list">{triage_rows}</div>
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
    has_full_history = sync_depth == "full_history"
    depth_label = "Full history" if has_full_history else "Latest message"
    history_state = _history_state_label(status, has_full_history, bool(item.get("needs_full_history")))
    card_class = _card_class(status, has_full_history)
    history_class = _history_pill_class(status, has_full_history, bool(item.get("needs_full_history")))
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
        selected_depth = "selected: full history" if has_full_history else "selected: latest only"
        actions.append(f"<span class='tag'>{selected_depth}</span>")
        if not has_full_history and item.get("needs_full_history"):
            actions.append(
                f"<form method='post' action='/admin/request-full-sync/{key}{token_query}'>"
                "<button class='ghost secondary' type='submit'>Get Full History First</button></form>"
            )
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
    follow_up = item.get("follow_up_date")
    next_action = html.escape(str(item.get("next_action") or "Review conversation."))
    if follow_up:
        next_action = f"{next_action}<div class='small'>Follow up: {html.escape(str(follow_up))}</div>"
    return f"""<article class="contact-card {card_class}">
  <div class="card-top">
    <div class="select-cell">{checkbox}</div>
    <div>
      <div class="contact-name">{html.escape(str(item.get("full_name") or "Unknown contact"))}</div>
      <div class="contact-meta">
        <span class="tag">score {html.escape(str(item.get("score") or 0))}</span>
        <span class="tag">{html.escape(str(item.get("group_category") or "uncategorized"))}</span>
        <span class="tag">{html.escape(str(item.get("conversation_time") or ""))}</span>
      </div>
    </div>
    <div class="status-stack">
      <span class="status-pill">{html.escape(decision_label)}</span>
      <span class="status-pill {history_class}">{html.escape(history_state)}</span>
      <span class="status-pill neutral">{html.escape(depth_label)}</span>
    </div>
  </div>
  <div class="card-body">
    <div>
      <div class="card-section-title">AI Readout</div>
      <div>{html.escape(str(item.get("relationship_stage") or ""))}</div>
      <div class="small">{html.escape(str(item.get("reply_owner") or ""))} · confidence {html.escape(str(item.get("confidence") or 0))}</div>
      <div class="tags" style="margin-top: 8px;">{reasons}</div>
    </div>
    <div>
      <div class="card-section-title">Next Action</div>
      <div class="next-action">{next_action}</div>
    </div>
    <div>
      <div class="card-section-title">Decision</div>
      <div class="action-rail">{" ".join(actions)}</div>
    </div>
  </div>
</article>"""


def _history_state_label(status: str, has_full_history: bool, needs_full_history: bool) -> str:
    if has_full_history:
        return "Full history ready"
    if status == "full_sync_requested":
        return "Waiting for Kondo full sync"
    if needs_full_history:
        return "Latest only - full history recommended"
    return "Latest only"


def _history_pill_class(status: str, has_full_history: bool, needs_full_history: bool) -> str:
    if has_full_history:
        return "done"
    if status == "full_sync_requested" or needs_full_history:
        return "warn"
    return "neutral"


def _card_class(status: str, has_full_history: bool) -> str:
    classes: list[str] = []
    if status == "staged_for_folk":
        classes.append("selected")
    if has_full_history:
        classes.append("full-ready")
    if status == "full_sync_requested":
        classes.append("waiting")
    if status == "excluded":
        classes.append("skipped")
    return " ".join(classes)


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
    last_event_at = max((str(row.get("updated_at") or "") for row in rows), default="")
    revision = "|".join(
        [
            last_event_at,
            str(summary["queue_depth"]),
            str(summary["needs_review"]),
            str(summary["selected"]),
            str(summary["waiting"]),
        ]
    )
    return {
        "mode": _mode_label(app_settings),
        "ai_provider": app_settings.ai_provider,
        "user": {"slug": active_user.slug, "name": active_user.name},
        "summary": summary,
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
    page = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kondo folk Sync Console</title>
  <style>
    :root {
      --bg: #f6f7f4;
      --surface: #fff;
      --ink: #151a18;
      --muted: #5f6b66;
      --line: #dde3df;
      --soft: #eef2ef;
      --green: #16673f;
      --green-soft: #e4f5ea;
      --amber: #ad6716;
      --amber-soft: #fff4df;
      --red: #9b2c2c;
      --blue: #285c8f;
      --shadow: 0 10px 30px rgba(20, 28, 24, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 20;
      background: rgba(255, 255, 255, .94);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--line);
    }
    .topbar-inner {
      max-width: 1440px;
      margin: 0 auto;
      padding: 16px 24px;
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
    }
    .brand h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    .brand p { margin: 3px 0 0; color: var(--muted); font-size: 13px; }
    .health-strip { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
    main { max-width: 1440px; margin: 0 auto; padding: 22px 24px 120px; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric span { display: block; color: var(--muted); font-size: 12px; font-weight: 650; }
    .metric strong { display: block; font-size: 24px; margin-top: 4px; }
    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      gap: 18px;
      align-items: start;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .filters { display: flex; flex-wrap: wrap; gap: 8px; }
    .search {
      min-width: 260px;
      flex: 1;
      max-width: 360px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
      background: var(--surface);
    }
    .review-list { display: grid; gap: 10px; }
    .contact-card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 1px 2px rgba(20, 28, 24, .04);
      overflow: hidden;
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    .contact-card.updated {
      border-color: var(--green);
      box-shadow: 0 0 0 3px rgba(22, 103, 63, .12), var(--shadow);
    }
    .contact-card.selected { border-color: #85b99c; box-shadow: inset 4px 0 0 var(--green); }
    .contact-card.waiting { box-shadow: inset 4px 0 0 var(--amber); }
    .contact-card.sent, .contact-card.skipped { opacity: .72; }
    .card-head {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      align-items: start;
      gap: 12px;
      padding: 16px;
      border-bottom: 1px solid var(--soft);
    }
    .name-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
    .contact-name { font-size: 16px; font-weight: 760; }
    .meta { margin-top: 5px; color: var(--muted); font-size: 13px; line-height: 1.35; }
    .pills { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
    .pill {
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--soft);
      color: #37423d;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .pill.green { background: var(--green-soft); color: var(--green); border-color: #b8dbc6; }
    .pill.amber { background: var(--amber-soft); color: var(--amber); border-color: #e8c27b; }
    .pill.blue { background: #e7f0fa; color: var(--blue); border-color: #bad0e7; }
    .pill.red { background: #fae8e8; color: var(--red); border-color: #edc4c4; }
    .card-body {
      display: grid;
      grid-template-columns: minmax(260px, 1.15fr) minmax(240px, 1fr) minmax(230px, .9fr);
      gap: 16px;
      padding: 16px;
    }
    .label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 760;
      text-transform: uppercase;
      letter-spacing: .03em;
      margin-bottom: 6px;
    }
    .body-text { font-size: 13px; line-height: 1.45; }
    .evidence { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; justify-content: flex-end; }
    .bucket-row { display: flex; gap: 8px; width: 100%; justify-content: flex-end; margin-top: 8px; }
    select {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 9px;
      background: var(--surface);
      font: inherit;
      font-size: 13px;
      max-width: 210px;
    }
    button, .button-link {
      border: 1px solid #18241f;
      background: #18241f;
      color: #fff;
      border-radius: 7px;
      padding: 9px 11px;
      font: inherit;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
    }
    button:hover, .button-link:hover { filter: brightness(.97); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .ghost { background: var(--surface); color: #18241f; border-color: var(--line); }
    .green-btn { background: var(--green); border-color: var(--green); }
    .amber-btn { background: var(--amber); border-color: var(--amber); }
    .danger { background: var(--red); border-color: var(--red); }
    .drawer {
      position: sticky;
      top: 90px;
      background: #14231d;
      color: #fff;
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .drawer-head { padding: 16px; border-bottom: 1px solid rgba(255,255,255,.12); }
    .drawer h2 { margin: 0; font-size: 18px; }
    .drawer .muted { color: #c7d2cc; }
    .batch-list { max-height: 420px; overflow: auto; }
    .batch-row {
      padding: 12px 16px;
      border-bottom: 1px solid rgba(255,255,255,.1);
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }
    .batch-row strong { display: block; font-size: 13px; }
    .drawer-actions { padding: 14px 16px; display: grid; gap: 8px; }
    .drawer button { width: 100%; }
    .notice {
      margin-bottom: 14px;
      padding: 11px 13px;
      border: 1px solid #b8dbc6;
      background: var(--green-soft);
      color: var(--green);
      border-radius: 8px;
      font-size: 14px;
    }
    .empty-state {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 26px;
      color: var(--muted);
    }
    .toast-stack {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 50;
      display: grid;
      gap: 8px;
      width: min(360px, calc(100vw - 36px));
    }
    .toast {
      background: #18241f;
      color: #fff;
      padding: 12px 14px;
      border-radius: 8px;
      box-shadow: var(--shadow);
      font-size: 13px;
    }
    .advanced {
      margin-top: 18px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    summary { cursor: pointer; font-weight: 700; }
    .advanced-actions, .reset-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    .reset-row input {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 9px 10px;
      font: inherit;
    }
    .small { color: var(--muted); font-size: 12px; margin-top: 4px; }
    .muted { color: var(--muted); }
    .hidden { display: none !important; }
    @media (max-width: 1080px) {
      .workspace { grid-template-columns: 1fr; }
      .drawer { position: static; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .topbar-inner { display: block; padding: 14px; }
      .health-strip { justify-content: flex-start; margin-top: 10px; }
      main { padding: 16px 14px 120px; }
      .metrics { grid-template-columns: 1fr; }
      .toolbar { display: block; }
      .filters { margin-top: 10px; }
      .search { min-width: 0; width: 100%; max-width: none; }
      .card-head, .card-body { grid-template-columns: 1fr; }
      .pills, .actions, .bucket-row { justify-content: flex-start; }
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <h1>Kondo folk Sync Console</h1>
        <p>__USER_NAME__ LinkedIn triage. Nothing goes to folk until it is in the selected batch.</p>
      </div>
      <div class="health-strip">
        <span class="pill">__MODE__</span>
        <span class="pill">ai=__AI__</span>
      </div>
    </div>
  </div>
  <main>
    <section id="notice" class="notice hidden"></section>
    <section class="metrics" id="metrics"></section>
    <section class="workspace">
      <div>
        <section class="toolbar">
          <div>
            <strong>Daily Review</strong>
            <div class="small" id="last-updated">Loading console state.</div>
          </div>
          <div class="filters">
            <input id="search" class="search" placeholder="Search name, company, bucket, LinkedIn">
            <button class="ghost" data-filter="review" type="button">Review</button>
            <button class="ghost" data-filter="full_ready" type="button">Full ready</button>
            <button class="ghost" data-filter="waiting" type="button">Waiting full</button>
            <button class="ghost" data-filter="selected" type="button">Selected</button>
            <button class="ghost" data-filter="sent" type="button">Sent</button>
            <button class="ghost" data-filter="skipped" type="button">Skipped</button>
          </div>
        </section>
        <section id="review-list" class="review-list"></section>
        <details class="advanced">
          <summary>Advanced queue tools</summary>
          <section class="advanced-actions">
            <button class="ghost" data-action="/process" type="button">Process Incoming Queue</button>
            <button class="ghost" data-action="/reconcile" type="button">Retry Failed/Due Work</button>
            <a class="button-link ghost" href="__STATS_HREF__" target="_blank" rel="noreferrer">JSON Stats</a>
          </section>
          <section class="reset-row">
            <input id="reset-confirm" placeholder="Type RESET">
            <button class="danger" id="reset-state" type="button">Clear Local Sync State</button>
          </section>
        </details>
      </div>
      <aside class="drawer">
        <div class="drawer-head">
          <h2>Selected Batch</h2>
          <div class="muted" id="batch-summary">Nothing selected.</div>
        </div>
        <div class="batch-list" id="batch-list"></div>
        <div class="drawer-actions">
          <button id="send-batch" class="green-btn" type="button" disabled>Send Selected Batch to folk</button>
          <button id="select-visible" class="ghost" type="button">Select Visible Latest</button>
        </div>
      </aside>
    </section>
  </main>
  <div class="toast-stack" id="toasts"></div>
  <script>
    const TOKEN = __TOKEN_JSON__;
    const NOTICE = __NOTICE_JSON__;
    const ADMIN_PREFIX = __ADMIN_PREFIX_JSON__;
    const stateUrl = () => `${ADMIN_PREFIX}/console-state${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""}`;
    const actionUrl = (path) => `${ADMIN_PREFIX}${path}${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""}`;
    let currentState = null;
    let activeFilter = "review";
    let searchTerm = "";
    let previousRows = new Map();

    const metricsEl = document.getElementById("metrics");
    const reviewListEl = document.getElementById("review-list");
    const batchListEl = document.getElementById("batch-list");
    const batchSummaryEl = document.getElementById("batch-summary");
    const sendBatchBtn = document.getElementById("send-batch");
    const lastUpdatedEl = document.getElementById("last-updated");
    const noticeEl = document.getElementById("notice");
    const toastsEl = document.getElementById("toasts");

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function toast(message) {
      const el = document.createElement("div");
      el.className = "toast";
      el.textContent = message;
      toastsEl.appendChild(el);
      setTimeout(() => el.remove(), 5200);
    }

    async function postAction(path, body = null) {
      const response = await fetch(actionUrl(path), { method: "POST", body });
      if (!response.ok) throw new Error(await response.text());
      await loadState(true);
    }

    function groupOption(value, label, selected) {
      return `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`;
    }

    function depthPill(row) {
      if (row.sync_depth === "full_history") return `<span class="pill green">Full history</span>`;
      if (row.needs_full_history) return `<span class="pill amber">Latest only - full recommended</span>`;
      return `<span class="pill">Latest message</span>`;
    }

    function statusPill(row) {
      const cls = row.console_state === "selected" ? "green" :
        row.console_state === "waiting" ? "amber" :
        row.console_state === "sent" ? "blue" :
        row.console_state === "skipped" ? "red" : "";
      return `<span class="pill ${cls}">${escapeHtml(row.console_label)}</span>`;
    }

    function rowMatches(row) {
      if (activeFilter !== "all" && row.console_state !== activeFilter) {
        if (!(activeFilter === "full_ready" && row.sync_depth === "full_history" && row.console_state !== "sent")) return false;
      }
      if (!searchTerm) return true;
      const haystack = [
        row.full_name,
        row.company,
        row.headline,
        row.linkedin_url,
        row.group_category,
        row.latest_message,
      ].join(" ").toLowerCase();
      return haystack.includes(searchTerm);
    }

    function actionButtons(row) {
      const key = encodeURIComponent(row.idempotency_key);
      const actions = [];
      if (row.console_state === "review" || row.console_state === "full_ready") {
        actions.push(`<button class="green-btn" data-post="/stage/${key}">${row.sync_depth === "full_history" ? "Select Full" : "Select Latest"}</button>`);
        if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full</button>`);
        actions.push(`<button class="ghost" data-post="/skip/${key}">Skip</button>`);
      } else if (row.console_state === "selected") {
        actions.push(`<button class="ghost" data-post="/unstage/${key}">Remove</button>`);
        if (row.sync_depth !== "full_history") actions.push(`<button class="amber-btn" data-post="/request-full-sync/${key}">Request Full</button>`);
      } else if (row.console_state === "waiting") {
        if (row.kondo_url) actions.push(`<a class="button-link amber-btn" href="${escapeHtml(row.kondo_url)}" target="_blank" rel="noreferrer">Open Kondo Full Sync</a>`);
        actions.push(`<button class="ghost" data-post="/stage/${key}">Use Latest Anyway</button>`);
      } else if (row.console_state === "sent") {
        actions.push(`<button class="ghost" data-post="/stage/${key}">Select to Resend</button>`);
      } else if (row.console_state === "skipped") {
        actions.push(`<button class="ghost" data-relevant="${key}">Mark Relevant</button>`);
      }
      if (row.linkedin_url) actions.push(`<a class="button-link ghost" href="${escapeHtml(row.linkedin_url)}" target="_blank" rel="noreferrer">LinkedIn</a>`);
      return actions.join("");
    }

    function cardClass(row) {
      const classes = ["contact-card", row.console_state];
      if (row.console_state === "selected") classes.push("selected");
      if (row.console_state === "waiting") classes.push("waiting");
      const previous = previousRows.get(row.idempotency_key);
      if (previous && previous.state !== row.console_state + row.sync_depth + row.updated_at) classes.push("updated");
      return classes.join(" ");
    }

    function renderCard(row) {
      const labels = (row.labels || []).slice(0, 3).map((label) => `<span class="pill">${escapeHtml(label)}</span>`).join("");
      const reasons = (row.reasons || []).slice(0, 5).map((reason) => `<span class="pill">${escapeHtml(reason)}</span>`).join("");
      const latest = row.latest_message ? escapeHtml(row.latest_message).slice(0, 260) : "No latest message captured.";
      const summary = row.summary ? escapeHtml(row.summary).slice(0, 260) : "No AI summary yet.";
      const meta = [row.headline, row.company, row.conversation_time].filter(Boolean).map(escapeHtml).join(" · ");
      return `<article class="${cardClass(row)}" data-key="${escapeHtml(row.idempotency_key)}">
        <div class="card-head">
          <div>
            <div class="name-line">
              <span class="contact-name">${escapeHtml(row.full_name || "Unknown contact")}</span>
              <span class="pill">score ${escapeHtml(row.score ?? 0)}</span>
            </div>
            <div class="meta">${meta}</div>
          </div>
          <div class="pills">
            ${statusPill(row)}
            ${depthPill(row)}
            <span class="pill blue">${escapeHtml(row.group_category || "uncategorized")}</span>
          </div>
        </div>
        <div class="card-body">
          <div>
            <div class="label">AI Readout</div>
            <div class="body-text">${summary}</div>
            <div class="evidence">${reasons || labels || "<span class='muted'>No evidence tags.</span>"}</div>
            <div class="small">${escapeHtml(row.relationship_stage || "")} · ${escapeHtml(row.reply_owner || "")} · confidence ${escapeHtml(row.confidence ?? 0)}</div>
          </div>
          <div>
            <div class="label">What happened</div>
            <div class="body-text">${latest}</div>
            <div class="small">Next: ${escapeHtml(row.next_action || "Review conversation.")}</div>
          </div>
          <div>
            <div class="label">Decision</div>
            <div class="actions">${actionButtons(row)}</div>
            <div class="bucket-row">
              <select data-group="${escapeHtml(row.idempotency_key)}">
                ${groupOption("claims_professionals", "Claims professional", row.group_category)}
                ${groupOption("distribution_partners", "Distribution partner", row.group_category)}
                ${groupOption("tpas_subrogation_attorneys", "TPA / subro attorney", row.group_category)}
              </select>
            </div>
          </div>
        </div>
      </article>`;
    }

    function renderMetrics(summary) {
      const cards = [
        ["Needs review", summary.needs_review || 0],
        ["Selected", summary.selected || 0],
        ["Full selected", summary.selected_full || 0],
        ["Waiting full", summary.waiting || 0],
        ["Queue", summary.queue_depth || 0],
      ];
      metricsEl.innerHTML = cards.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
    }

    function renderBatch(rows, summary) {
      const selected = rows.filter((row) => row.console_state === "selected");
      sendBatchBtn.disabled = selected.length === 0;
      batchSummaryEl.textContent = selected.length
        ? `${selected.length} selected: ${summary.selected_latest || 0} latest, ${summary.selected_full || 0} full`
        : "Nothing selected.";
      if (!selected.length) {
        batchListEl.innerHTML = `<div class="batch-row"><span class="muted">Select contacts from the review list. folk will not be touched until you send this batch.</span></div>`;
        return;
      }
      batchListEl.innerHTML = selected.map((row) => `<div class="batch-row">
        <div>
          <strong>${escapeHtml(row.full_name || "Unknown contact")}</strong>
          <span class="muted">${row.sync_depth === "full_history" ? "Full conversation" : "Latest message"}</span>
        </div>
        <button class="ghost" data-post="/unstage/${encodeURIComponent(row.idempotency_key)}">Remove</button>
      </div>`).join("");
    }

    function renderState(state) {
      currentState = state;
      renderMetrics(state.summary || {});
      renderBatch(state.rows || [], state.summary || {});
      lastUpdatedEl.textContent = state.last_event_at ? `Last Kondo update: ${state.last_event_at}` : "Waiting for Kondo activity.";
      const rows = (state.rows || []).filter(rowMatches);
      reviewListEl.innerHTML = rows.length ? rows.map(renderCard).join("") : `<section class="empty-state">No contacts match this view.</section>`;
      const nextPrevious = new Map();
      for (const row of state.rows || []) nextPrevious.set(row.idempotency_key, { state: row.console_state + row.sync_depth + row.updated_at });
      previousRows = nextPrevious;
    }

    async function loadState(showToast = false) {
      const response = await fetch(stateUrl());
      if (!response.ok) throw new Error(await response.text());
      const nextState = await response.json();
      if (currentState && currentState.revision !== nextState.revision) {
        const oldRows = new Map((currentState.rows || []).map((row) => [row.idempotency_key, row]));
        for (const row of nextState.rows || []) {
          const old = oldRows.get(row.idempotency_key);
          if (old && old.sync_depth !== "full_history" && row.sync_depth === "full_history") toast(`Full history ready for ${row.full_name || "contact"}`);
          else if (!old && row.console_state !== "skipped") toast(`Kondo sync received for ${row.full_name || "contact"}`);
        }
      } else if (showToast) {
        toast("Console updated.");
      }
      renderState(nextState);
    }

    document.addEventListener("click", async (event) => {
      const target = event.target.closest("button, a");
      if (!target) return;
      if (target.dataset.filter) {
        activeFilter = target.dataset.filter;
        renderState(currentState);
        return;
      }
      if (target.dataset.action) {
        event.preventDefault();
        try { await postAction(target.dataset.action); toast("Queue action started."); } catch (error) { toast(error.message); }
        return;
      }
      if (target.dataset.post) {
        event.preventDefault();
        try { await postAction(target.dataset.post); toast("Updated selection."); } catch (error) { toast(error.message); }
        return;
      }
      if (target.dataset.relevant) {
        event.preventDefault();
        const body = new FormData();
        const row = currentState.rows.find((item) => item.idempotency_key === decodeURIComponent(target.dataset.relevant));
        body.append("group_category", row?.group_category || "claims_professionals");
      try { await postAction(`/mark-relevant/${target.dataset.relevant}`, body); toast("Marked relevant."); } catch (error) { toast(error.message); }
      }
    });

    document.getElementById("search").addEventListener("input", (event) => {
      searchTerm = event.target.value.toLowerCase().trim();
      renderState(currentState);
    });

    document.addEventListener("change", async (event) => {
      const target = event.target;
      if (!target.dataset || !target.dataset.group) return;
      const body = new FormData();
      body.append("group_category", target.value);
      try { await postAction(`/group/${encodeURIComponent(target.dataset.group)}`, body); toast("Bucket updated."); } catch (error) { toast(error.message); }
    });

    sendBatchBtn.addEventListener("click", async () => {
      try { await postAction("/send-staged"); toast("Selected batch queued for folk."); } catch (error) { toast(error.message); }
    });

    document.getElementById("select-visible").addEventListener("click", async () => {
      if (!currentState) return;
      const visible = currentState.rows.filter(rowMatches).filter((row) => row.console_state === "review" || row.console_state === "full_ready");
      for (const row of visible) await postAction(`/stage/${encodeURIComponent(row.idempotency_key)}`);
      toast(`Selected ${visible.length} visible contact(s).`);
    });

    document.getElementById("reset-state").addEventListener("click", async () => {
      const body = new FormData();
      body.append("confirm", document.getElementById("reset-confirm").value);
      try { await postAction("/reset-local-state", body); toast("Local sync state cleared."); } catch (error) { toast(error.message); }
    });

    if (NOTICE) {
      noticeEl.textContent = NOTICE;
      noticeEl.classList.remove("hidden");
    }
    loadState().catch((error) => toast(error.message));
    setInterval(() => loadState().catch((error) => toast(error.message)), 3000);
  </script>
</body>
</html>"""
    return (
        page.replace("__TOKEN_JSON__", json.dumps(token or ""))
        .replace("__NOTICE_JSON__", json.dumps(notice or ""))
        .replace("__MODE__", html.escape(_mode_label(app_settings)))
        .replace("__AI__", html.escape(app_settings.ai_provider))
        .replace("__STATS_HREF__", html.escape(stats_href))
        .replace("__USER_NAME__", html.escape(active_user.name))
        .replace("__ADMIN_PREFIX_JSON__", json.dumps(admin_prefix))
    )


app = create_app()
