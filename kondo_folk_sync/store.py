from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class SyncStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                create table if not exists processed_events (
                    idempotency_key text primary key,
                    linkedin_url text,
                    status text not null,
                    payload_json text not null,
                    analysis_json text,
                    result_json text,
                    error text,
                    attempts integer not null default 0,
                    next_attempt_at text,
                    locked_at text,
                    created_at text not null,
                    updated_at text not null
                );

                create table if not exists people_map (
                    linkedin_url text primary key,
                    folk_person_id text not null,
                    folk_note_id text,
                    created_at text not null,
                    updated_at text not null
                );
                """
            )
            self._ensure_column(conn, "processed_events", "attempts", "integer not null default 0")
            self._ensure_column(conn, "processed_events", "next_attempt_at", "text")
            self._ensure_column(conn, "processed_events", "locked_at", "text")
            self._ensure_column(conn, "processed_events", "stage_from_status", "text")
            self._ensure_column(conn, "processed_events", "manual_override_json", "text")
            self._ensure_column(conn, "processed_events", "user_slug", "text not null default 'default'")
            self._ensure_column(conn, "people_map", "folk_note_id", "text")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute(f"pragma table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def get_event(self, idempotency_key: str, user_slug: str = "default") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            ).fetchone()
        return dict(row) if row else None

    def get_event_payload(self, idempotency_key: str, user_slug: str = "default") -> dict[str, Any] | None:
        event = self.get_event(idempotency_key, user_slug=user_slug)
        if not event:
            return None
        return json.loads(str(event["payload_json"]))

    def get_event_analysis(self, idempotency_key: str, user_slug: str = "default") -> dict[str, Any] | None:
        event = self.get_event(idempotency_key, user_slug=user_slug)
        if not event or not event.get("analysis_json"):
            return None
        return _loads_dict(event["analysis_json"])

    def delete_event(self, idempotency_key: str, user_slug: str = "default") -> None:
        with self._connect() as conn:
            conn.execute(
                "delete from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            )

    def reset_all(self) -> None:
        with self._connect() as conn:
            conn.execute("delete from processed_events")
            conn.execute("delete from people_map")

    def reset_user(self, user_slug: str) -> None:
        with self._connect() as conn:
            conn.execute("delete from processed_events where user_slug = ?", (user_slug,))

    def queue_event(
        self,
        idempotency_key: str,
        linkedin_url: str | None,
        payload: dict[str, Any],
        force: bool = False,
        user_slug: str = "default",
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "select * from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            ).fetchone()
            if existing and not force:
                return dict(existing)
            if existing and force:
                conn.execute(
                    """
                    update processed_events
                    set linkedin_url = ?, status = ?, payload_json = ?, analysis_json = null,
                        result_json = null, error = null, attempts = 0, next_attempt_at = null,
                        locked_at = null, user_slug = ?, updated_at = ?
                    where idempotency_key = ? and user_slug = ?
                    """,
                    (
                        linkedin_url,
                        "queued",
                        json.dumps(payload, sort_keys=True, default=str),
                        user_slug,
                        now,
                        idempotency_key,
                        user_slug,
                    ),
                )
            else:
                conn.execute(
                    """
                    insert into processed_events (
                        idempotency_key, linkedin_url, status, payload_json, user_slug, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        linkedin_url,
                        "queued",
                        json.dumps(payload, sort_keys=True, default=str),
                        user_slug,
                        now,
                        now,
                    ),
                )
            row = conn.execute(
                "select * from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            ).fetchone()
        return dict(row)

    def start_event(
        self,
        idempotency_key: str,
        linkedin_url: str | None,
        payload: dict[str, Any],
        user_slug: str = "default",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into processed_events (
                    idempotency_key, linkedin_url, status, payload_json, user_slug, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    linkedin_url,
                    "processing",
                    json.dumps(payload, sort_keys=True, default=str),
                    user_slug,
                    now,
                    now,
                ),
            )

    def mark_processing(self, idempotency_key: str, user_slug: str = "default") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = 'processing', attempts = attempts + 1, locked_at = ?, updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (now, now, idempotency_key, user_slug),
            )

    def finish_event(
        self,
        idempotency_key: str,
        status: str,
        analysis: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        user_slug: str = "default",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = ?, analysis_json = ?, result_json = ?, error = ?,
                    next_attempt_at = null, locked_at = null, updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (
                    status,
                    json.dumps(analysis, sort_keys=True, default=str) if analysis else None,
                    json.dumps(result, sort_keys=True, default=str) if result else None,
                    error,
                    now,
                    idempotency_key,
                    user_slug,
                ),
            )

    def defer_event(
        self,
        idempotency_key: str,
        error: str,
        next_attempt_at: datetime,
        user_slug: str = "default",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = 'retry_wait', error = ?, next_attempt_at = ?, locked_at = null,
                    updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (error, next_attempt_at.astimezone(UTC).isoformat(), now, idempotency_key, user_slug),
            )

    def stage_for_folk(self, idempotency_key: str, user_slug: str = "default") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = 'staged_for_folk', error = null, next_attempt_at = null,
                    locked_at = null, stage_from_status = status, updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (now, idempotency_key, user_slug),
            )

    def stage_many_for_folk(self, idempotency_keys: list[str], user_slug: str = "default") -> int:
        if not idempotency_keys:
            return 0
        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("?" for _ in idempotency_keys)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                update processed_events
                set status = 'staged_for_folk', error = null, next_attempt_at = null,
                    locked_at = null, stage_from_status = status, updated_at = ?
                where idempotency_key in ({placeholders})
                  and user_slug = ?
                  and analysis_json is not null
                  and status != 'excluded'
                """,
                (now, *idempotency_keys, user_slug),
            )
        return int(cursor.rowcount)

    def stage_all_for_folk(self, user_slug: str = "default") -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update processed_events
                set status = 'staged_for_folk', error = null, next_attempt_at = null,
                    locked_at = null, stage_from_status = status, updated_at = ?
                where status = 'review_pending'
                  and user_slug = ?
                  and analysis_json is not null
                """,
                (now, user_slug),
            )
        return int(cursor.rowcount)

    def queue_staged_for_folk(self, user_slug: str = "default") -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                update processed_events
                set status = 'queued_for_folk', error = null, next_attempt_at = null,
                    locked_at = null, stage_from_status = null, updated_at = ?
                where status = 'staged_for_folk'
                  and user_slug = ?
                  and analysis_json is not null
                """,
                (now, user_slug),
            )
        return int(cursor.rowcount)

    def unstage_for_folk(self, idempotency_key: str, user_slug: str = "default") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = coalesce(stage_from_status, 'review_pending'),
                    stage_from_status = null,
                    updated_at = ?
                where idempotency_key = ?
                  and user_slug = ?
                  and status = 'staged_for_folk'
                """,
                (now, idempotency_key, user_slug),
            )

    def request_full_sync(self, idempotency_key: str, user_slug: str = "default") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = 'full_sync_requested', updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (now, idempotency_key, user_slug),
            )

    def skip_event(self, idempotency_key: str, user_slug: str = "default") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                update processed_events
                set status = 'excluded',
                    result_json = ?,
                    error = null,
                    next_attempt_at = null,
                    locked_at = null,
                    stage_from_status = null,
                    updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (
                    json.dumps(
                        {
                            "status": "excluded",
                            "reason": "manual_console_skip",
                        },
                        sort_keys=True,
                    ),
                    now,
                    idempotency_key,
                    user_slug,
                ),
            )

    def mark_relevant(
        self,
        idempotency_key: str,
        group_category: str | None = None,
        user_slug: str = "default",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        allowed_groups = {
            "claims_professionals",
            "distribution_partners",
            "tpas_subrogation_attorneys",
        }
        with self._connect() as conn:
            row = conn.execute(
                "select analysis_json from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            ).fetchone()
            if not row:
                return
            analysis = _loads_dict(row["analysis_json"])
            if group_category in allowed_groups:
                analysis["group_category"] = group_category
                analysis["group_reason"] = "Manually selected in the review console."
            if str(analysis.get("relationship_stage") or "") == "not_relevant":
                analysis["relationship_stage"] = "active_conversation"
                analysis["next_action"] = "Review this relevant contact and decide whether to send to folk."
            override = {
                "marked_relevant": True,
                "group_category": analysis.get("group_category"),
                "updated_at": now,
            }
            conn.execute(
                """
                update processed_events
                set status = 'review_pending',
                    analysis_json = ?,
                    manual_override_json = ?,
                    result_json = ?,
                    error = null,
                    next_attempt_at = null,
                    locked_at = null,
                    stage_from_status = null,
                    updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (
                    json.dumps(analysis, sort_keys=True, default=str),
                    json.dumps(override, sort_keys=True, default=str),
                    json.dumps(
                        {
                            "status": "review_pending",
                            "reason": "manual_console_reinclude",
                        },
                        sort_keys=True,
                    ),
                    now,
                    idempotency_key,
                    user_slug,
                ),
            )

    def update_group_category(
        self,
        idempotency_key: str,
        group_category: str,
        user_slug: str = "default",
    ) -> None:
        allowed_groups = {
            "claims_professionals",
            "distribution_partners",
            "tpas_subrogation_attorneys",
        }
        if group_category not in allowed_groups:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "select analysis_json, manual_override_json from processed_events where idempotency_key = ? and user_slug = ?",
                (idempotency_key, user_slug),
            ).fetchone()
            if not row:
                return
            analysis = _loads_dict(row["analysis_json"])
            analysis["group_category"] = group_category
            analysis["group_reason"] = "Manually selected in the review console."
            override = _loads_dict(row["manual_override_json"])
            override.update({"group_category": group_category, "updated_at": now})
            conn.execute(
                """
                update processed_events
                set analysis_json = ?,
                    manual_override_json = ?,
                    updated_at = ?
                where idempotency_key = ? and user_slug = ?
                """,
                (
                    json.dumps(analysis, sort_keys=True, default=str),
                    json.dumps(override, sort_keys=True, default=str),
                    now,
                    idempotency_key,
                    user_slug,
                ),
            )

    def auto_stage_full_history_if_latest_selected(
        self,
        linkedin_url: str | None,
        full_history_key: str,
        user_slug: str = "default",
    ) -> bool:
        if not linkedin_url:
            return False
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            full_row = conn.execute(
                """
                select status, stage_from_status
                from processed_events
                where idempotency_key = ? and user_slug = ?
                """,
                (full_history_key, user_slug),
            ).fetchone()
            if not full_row:
                return False
            same_row_was_selected = bool(full_row["stage_from_status"])
            selected_latest = conn.execute(
                """
                select idempotency_key
                from processed_events
                where linkedin_url = ?
                  and idempotency_key != ?
                  and user_slug = ?
                  and status = 'staged_for_folk'
                limit 1
                """,
                (linkedin_url, full_history_key, user_slug),
            ).fetchone()
            if not same_row_was_selected and not selected_latest:
                return False
            if selected_latest:
                conn.execute(
                    """
                    update processed_events
                    set status = coalesce(stage_from_status, 'review_pending'),
                        stage_from_status = null,
                        updated_at = ?
                    where idempotency_key = ?
                      and user_slug = ?
                    """,
                    (now, selected_latest["idempotency_key"], user_slug),
                )
            conn.execute(
                """
                update processed_events
                set status = 'staged_for_folk',
                    stage_from_status = coalesce(stage_from_status, 'review_pending'),
                    updated_at = ?
                where idempotency_key = ?
                  and user_slug = ?
                """,
                (now, full_history_key, user_slug),
            )
        return True

    def get_person_id(self, linkedin_url: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select folk_person_id from people_map where linkedin_url = ?",
                (linkedin_url,),
            ).fetchone()
        return str(row["folk_person_id"]) if row else None

    def get_note_id(self, linkedin_url: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "select folk_note_id from people_map where linkedin_url = ?",
                (linkedin_url,),
            ).fetchone()
        if not row or row["folk_note_id"] is None:
            return None
        return str(row["folk_note_id"])

    def set_person_id(self, linkedin_url: str, folk_person_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into people_map (linkedin_url, folk_person_id, created_at, updated_at)
                values (?, ?, ?, ?)
                on conflict(linkedin_url) do update set
                    folk_person_id = excluded.folk_person_id,
                    updated_at = excluded.updated_at
                """,
                (linkedin_url, folk_person_id, now, now),
            )

    def set_note_id(self, linkedin_url: str, folk_note_id: str) -> None:
        folk_person_id = self.get_person_id(linkedin_url)
        if not folk_person_id:
            raise RuntimeError("Cannot store folk note id before person id is mapped")
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert into people_map (linkedin_url, folk_person_id, folk_note_id, created_at, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(linkedin_url) do update set
                    folk_note_id = excluded.folk_note_id,
                    updated_at = excluded.updated_at
                """,
                (linkedin_url, folk_person_id, folk_note_id, now, now),
            )

    def recent_events(self, limit: int = 50, user_slug: str | None = None) -> list[dict[str, Any]]:
        where = "where user_slug = ?" if user_slug else ""
        params: tuple[Any, ...] = (user_slug, limit) if user_slug else (limit,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select idempotency_key, linkedin_url, status, error, attempts, next_attempt_at,
                    user_slug, created_at, updated_at
                from processed_events
                {where}
                order by created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def priority_events(self, limit: int = 25, user_slug: str = "default") -> list[dict[str, Any]]:
        items = self.triage_events(limit=max(limit * 4, limit), sort_by="score", user_slug=user_slug)
        return [item for item in items if item["score"] > 0][:limit]

    def triage_events(
        self,
        limit: int = 100,
        since_hours: int = 0,
        sort_by: str = "conversation_time",
        user_slug: str = "default",
    ) -> list[dict[str, Any]]:
        since = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat() if since_hours > 0 else None
        with self._connect() as conn:
            rows = conn.execute(
                """
                select idempotency_key, linkedin_url, status, payload_json, analysis_json,
                    manual_override_json, user_slug, updated_at
                from processed_events
                where status in (
                    'review_pending', 'full_sync_requested', 'queued_for_folk',
                    'staged_for_folk',
                    'synced', 'dry_run', 'excluded'
                )
                  and user_slug = ?
                  and analysis_json is not null
                order by updated_at desc
                limit ?
                """,
                (user_slug, max(limit * 4, limit, 1)),
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads_dict(row["payload_json"])
            analysis = _loads_dict(row["analysis_json"])
            item = _priority_item(dict(row), payload, analysis)
            if since is None or str(item.get("conversation_time") or "") >= since:
                items.append(item)
        if sort_by == "score":
            items.sort(key=lambda item: (item["score"], item["conversation_time"], item["updated_at"]), reverse=True)
        else:
            items.sort(key=lambda item: (item["conversation_time"], item["updated_at"]), reverse=True)
        return items[:limit]

    def status_counts(self, user_slug: str | None = None) -> dict[str, int]:
        where = "where user_slug = ?" if user_slug else ""
        params: tuple[Any, ...] = (user_slug,) if user_slug else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select status, count(*) as count
                from processed_events
                {where}
                group by status
                order by status
                """,
                params,
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def retryable_events(
        self,
        limit: int = 25,
        processing_timeout_seconds: int = 120,
        user_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        stale_processing = (datetime.now(UTC) - timedelta(seconds=processing_timeout_seconds)).isoformat()
        user_filter = "and user_slug = ?" if user_slug else ""
        params: tuple[Any, ...] = (
            stale_processing,
            now,
            user_slug,
            limit,
        ) if user_slug else (stale_processing, now, limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select idempotency_key, linkedin_url, status, error, attempts, next_attempt_at,
                    user_slug, created_at, updated_at
                from processed_events
                where (
                   status = 'error'
                   or status = 'queued_for_folk'
                   or (status = 'processing' and (locked_at is null or locked_at <= ?))
                   or (status = 'retry_wait' and (next_attempt_at is null or next_attempt_at <= ?))
                )
                   {user_filter}
                order by updated_at asc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def next_queued_event(
        self,
        processing_timeout_seconds: int = 120,
        user_slug: str | None = None,
    ) -> dict[str, Any] | None:
        now = datetime.now(UTC).isoformat()
        stale_processing = (datetime.now(UTC) - timedelta(seconds=processing_timeout_seconds)).isoformat()
        user_filter = "and user_slug = ?" if user_slug else ""
        params: tuple[Any, ...] = (
            stale_processing,
            now,
            user_slug,
        ) if user_slug else (stale_processing, now)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                select *
                from processed_events
                where (
                   status = 'queued'
                   or status = 'queued_for_folk'
                   or status = 'error'
                   or (status = 'processing' and (locked_at is null or locked_at <= ?))
                   or (status = 'retry_wait' and (next_attempt_at is null or next_attempt_at <= ?))
                )
                   {user_filter}
                order by created_at asc
                limit 1
                """,
                params,
            ).fetchone()
        return dict(row) if row else None

    def queue_depth(self, processing_timeout_seconds: int = 120, user_slug: str | None = None) -> int:
        now = datetime.now(UTC).isoformat()
        stale_processing = (datetime.now(UTC) - timedelta(seconds=processing_timeout_seconds)).isoformat()
        user_filter = "and user_slug = ?" if user_slug else ""
        params: tuple[Any, ...] = (
            stale_processing,
            now,
            user_slug,
        ) if user_slug else (stale_processing, now)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                select count(*) as count
                from processed_events
                where (
                   status = 'queued'
                   or status = 'queued_for_folk'
                   or status = 'error'
                   or (status = 'processing' and (locked_at is null or locked_at <= ?))
                   or (status = 'retry_wait' and (next_attempt_at is null or next_attempt_at <= ?))
                )
                   {user_filter}
                """,
                params,
            ).fetchone()
        return int(row["count"]) if row else 0


def _loads_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _priority_item(
    row: dict[str, Any],
    payload: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    status = str(row.get("status") or "")
    conversation_text = str(payload.get("conversation_text") or "")
    has_full_history = bool(payload.get("full_history_available")) or "\n" in conversation_text
    sync_depth = "full_history" if has_full_history else "latest_message"
    relationship_stage = str(analysis.get("relationship_stage") or "")
    reply_owner = str(analysis.get("reply_owner") or "")
    group_category = str(analysis.get("group_category") or "")
    follow_up_date = analysis.get("follow_up_date") or None
    meeting_detected = bool(analysis.get("meeting_detected") or False)
    try:
        confidence = float(analysis.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    if relationship_stage == "meeting_booked":
        score += 5
        reasons.append("meeting booked")
    elif relationship_stage in {"needs_follow_up", "active_conversation"}:
        score += 2
        reasons.append(relationship_stage.replace("_", " "))

    if reply_owner == "user_owes_reply":
        score += 4
        reasons.append("you owe reply")
    if follow_up_date:
        score += 3
        reasons.append("follow-up date")
    if meeting_detected:
        score += 2
        reasons.append("meeting signal")
    if group_category in {"claims_professionals", "distribution_partners", "tpas_subrogation_attorneys"}:
        score += 1
    if confidence >= 0.75:
        score += 1

    if status == "excluded":
        score = 0
        reasons = ["excluded"]

    manual_override = _loads_dict(row.get("manual_override_json"))
    if manual_override:
        reasons.append("manual override")

    needs_full_history = (
        status != "excluded"
        and (
        score >= 4
        or relationship_stage in {"meeting_booked", "needs_follow_up"}
        or reply_owner == "user_owes_reply"
        )
    )
    if needs_full_history:
        reasons.append("needs full history")

    return {
        "idempotency_key": row["idempotency_key"],
        "user_slug": row.get("user_slug") or "default",
        "status": status,
        "sync_depth": sync_depth,
        "has_full_history": has_full_history,
        "linkedin_url": row.get("linkedin_url") or payload.get("linkedin_url"),
        "kondo_url": payload.get("kondo_url"),
        "full_name": payload.get("full_name") or row.get("linkedin_url") or "Unknown contact",
        "headline": payload.get("headline"),
        "company": payload.get("company"),
        "location": payload.get("location"),
        "labels": payload.get("kondo_labels") or [],
        "latest_message": payload.get("latest_message"),
        "latest_message_direction": payload.get("latest_message_direction") or "unknown",
        "conversation_status": payload.get("conversation_status"),
        "summary": analysis.get("summary") or "",
        "group_reason": analysis.get("group_reason") or "",
        "group_category": group_category,
        "relationship_stage": relationship_stage,
        "reply_owner": reply_owner,
        "next_action": analysis.get("next_action") or "Review conversation.",
        "follow_up_date": follow_up_date,
        "meeting_detected": meeting_detected,
        "confidence": round(confidence, 2),
        "score": score,
        "reasons": reasons,
        "needs_full_history": needs_full_history,
        "conversation_time": payload.get("latest_conversation_timestamp") or row.get("updated_at"),
        "updated_at": row.get("updated_at"),
        "manual_override": manual_override,
    }
