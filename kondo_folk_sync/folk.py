from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Settings
from .models import AIAnalysis, NormalizedKondoEvent
from .store import SyncStore


class FolkRateLimitError(RuntimeError):
    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class FolkClient:
    def __init__(self, settings: Settings, store: SyncStore) -> None:
        self.settings = settings
        self.store = store

    async def sync(self, event: NormalizedKondoEvent, analysis: AIAnalysis) -> dict[str, Any]:
        if not event.linkedin_url:
            return {"status": "held_for_review", "reason": "missing_linkedin_url"}

        if self.settings.dry_run:
            return {
                "status": "dry_run",
                "would_write": self._planned_writes(event, analysis),
            }

        if not self.settings.folk_api_key:
            raise RuntimeError("FOLK_API_KEY is required when KONDO_FOLK_DRY_RUN=false")

        async with httpx.AsyncClient(
            base_url=self.settings.folk_base_url,
            headers={"Authorization": f"Bearer {self.settings.folk_api_key}"},
            timeout=30,
        ) as client:
            person_id = await self._upsert_person(client, event, analysis)
            interaction = await self._create_interaction(client, person_id, event, analysis)
            note = await self._upsert_note(client, person_id, event, analysis)
            reminder = None
            reminder_error = None
            if self._should_create_reminder(analysis):
                try:
                    reminder = await self._create_reminder(client, person_id, event, analysis)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 422:
                        raise
                    reminder_error = str(exc)

        return {
            "status": "synced",
            "folk_person_id": person_id,
            "folk_group_id": self._group_id_for_analysis(analysis),
            "folk_group_category": analysis.group_category,
            "interaction_id": interaction.get("id"),
            "note_id": note.get("id"),
            "reminder_id": reminder.get("id") if reminder else None,
            "reminder_error": reminder_error,
        }

    def _planned_writes(self, event: NormalizedKondoEvent, analysis: AIAnalysis) -> dict[str, Any]:
        return {
            "person": self._person_payload(event, analysis),
            "interaction": self._interaction_payload("per_dry_run", event, analysis),
            "note": self._note_payload("per_dry_run", event, analysis),
            "reminder": self._reminder_payload("per_dry_run", event, analysis)
            if self._should_create_reminder(analysis)
            else None,
        }

    async def _upsert_person(
        self,
        client: httpx.AsyncClient,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> str:
        existing = self.store.get_person_id(event.linkedin_url or "")
        payload = self._person_payload(event, analysis)
        if existing:
            response = await self._request(client, "PATCH", f"/people/{existing}", json=payload)
            return existing

        response = await self._request(client, "POST", "/people", json=payload)
        person_id = response.json()["data"]["id"]
        self.store.set_person_id(event.linkedin_url or "", person_id)
        return person_id

    def _person_payload(
        self,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fullName": event.full_name or "LinkedIn contact",
            "description": "Synced from Kondo LinkedIn Sales Navigator.",
            "urls": [event.linkedin_url] if event.linkedin_url else [],
        }
        if event.headline:
            payload["jobTitle"] = event.headline[:500]
        group_id = self._group_id_for_analysis(analysis)
        if group_id:
            payload["groups"] = [{"id": group_id}]
            if analysis:
                payload["customFieldValues"] = {
                    group_id: {
                        "Status": _status_for_analysis(analysis),
                    }
                }
        return payload

    def _group_id_for_analysis(self, analysis: AIAnalysis | None) -> str | None:
        category = analysis.group_category if analysis else None
        group_ids = {
            "claims_professionals": self.settings.folk_claims_professionals_group_id,
            "distribution_partners": self.settings.folk_distribution_partners_group_id,
            "tpas_subrogation_attorneys": self.settings.folk_tpas_subrogation_attorneys_group_id,
        }
        if category and group_ids.get(category):
            return group_ids[category]
        return self.settings.folk_group_id

    async def _create_interaction(
        self,
        client: httpx.AsyncClient,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        response = await self._request(
            client,
            "POST",
            "/interactions",
            json=self._interaction_payload(person_id, event, analysis),
        )
        return response.json()["data"]

    def _interaction_payload(
        self,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        return {
            "entity": {"id": person_id},
            "dateTime": _folk_datetime(event.latest_conversation_timestamp),
            "title": "LinkedIn conversation synced from Kondo",
            "content": _crm_content(event, analysis),
            "type": "💬",
        }

    async def _upsert_note(
        self,
        client: httpx.AsyncClient,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        note_payload = self._note_payload(person_id, event, analysis)
        note_id = self.store.get_note_id(event.linkedin_url or "")
        if note_id:
            try:
                response = await self._request(client, "PATCH", f"/notes/{note_id}", json=note_payload)
                return response.json()["data"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise

        response = await self._request(client, "POST", "/notes", json=note_payload)
        note = response.json()["data"]
        if event.linkedin_url and note.get("id"):
            self.store.set_note_id(event.linkedin_url, str(note["id"]))
        return note

    def _note_payload(
        self,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        return {
            "entity": {"id": person_id},
            "visibility": "private",
            "content": _crm_content(event, analysis),
        }

    def _should_create_reminder(self, analysis: AIAnalysis) -> bool:
        return (
            analysis.confidence >= 0.5
            and analysis.reply_owner == "user_owes_reply"
            and bool(analysis.follow_up_date)
            and _future_date(analysis.follow_up_date)
        )

    async def _create_reminder(
        self,
        client: httpx.AsyncClient,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        response = await self._request(
            client,
            "POST",
            "/reminders",
            json=self._reminder_payload(person_id, event, analysis),
        )
        return response.json()["data"]

    def _reminder_payload(
        self,
        person_id: str,
        event: NormalizedKondoEvent,
        analysis: AIAnalysis,
    ) -> dict[str, Any]:
        reminder: dict[str, Any] = {
            "entity": {"id": person_id},
            "name": f"Follow up with {event.full_name or 'LinkedIn contact'}",
            "recurrenceRule": _single_reminder_rule(
                analysis.follow_up_date or datetime.now().date().isoformat(),
                self.settings.default_timezone,
                self.settings.default_followup_hour,
            ),
            "visibility": self.settings.folk_reminder_visibility,
        }
        if self.settings.folk_reminder_visibility == "public":
            if not self.settings.folk_assigned_user_email:
                raise RuntimeError("FOLK_ASSIGNED_USER_EMAIL is required for public reminders")
            reminder["assignedUsers"] = [{"email": self.settings.folk_assigned_user_email}]
        return reminder

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        if self.settings.folk_request_spacing_seconds > 0:
            await asyncio.sleep(self.settings.folk_request_spacing_seconds)
        response = await client.request(method, url, **kwargs)
        if response.status_code == 429:
            raise FolkRateLimitError(
                f"folk rate limit reached for {method} {url}",
                _retry_at(response),
            )
        response.raise_for_status()
        return response


def _crm_content(event: NormalizedKondoEvent, analysis: AIAnalysis) -> str:
    sections = [
        "# Kondo LinkedIn Sync",
        f"**Summary:** {analysis.summary}",
        f"**CRM note:** {analysis.crm_note}",
        f"**folk group:** {analysis.group_category}",
        f"**folk status:** {_status_for_analysis(analysis)}",
        f"**Stage:** {analysis.relationship_stage}",
        f"**Reply owner:** {analysis.reply_owner}",
        f"**Next action:** {analysis.next_action}",
    ]
    if analysis.group_reason:
        sections.append(f"**Group reason:** {analysis.group_reason}")
    if analysis.follow_up_date:
        sections.append(f"**Follow-up date:** {analysis.follow_up_date}")
    if event.latest_message:
        sections.append(f"**Latest message:** {event.latest_message}")
    if event.kondo_url:
        sections.append(f"**Kondo:** {event.kondo_url}")
    if event.linkedin_url:
        sections.append(f"**LinkedIn:** {event.linkedin_url}")
    if analysis.important_context:
        sections.append("**Context:**\n" + "\n".join(f"- {item}" for item in analysis.important_context))
    return "\n\n".join(sections)


def _status_for_analysis(analysis: AIAnalysis) -> str:
    if analysis.relationship_stage in {"closed_lost", "not_relevant"}:
        return "Closed-lost"
    if analysis.reply_owner == "user_owes_reply" or analysis.relationship_stage == "needs_follow_up":
        return "Follow-up"
    if analysis.relationship_stage == "meeting_booked":
        return "Qualified"
    return "Lead"


def _folk_datetime(value: str | None) -> str:
    if not value:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _single_reminder_rule(date_value: str, timezone: str, hour: int) -> str:
    compact = date_value.replace("-", "")
    return f"DTSTART;TZID={timezone}:{compact}T{hour:02d}0000\nRRULE:COUNT=1"


def _future_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value).date()
    except ValueError:
        return False
    return parsed > datetime.now(UTC).date()


def _retry_at(response: httpx.Response) -> datetime:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return datetime.now(UTC) + timedelta(seconds=max(1, int(float(retry_after))))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                return parsed.astimezone(UTC)
            except (TypeError, ValueError):
                pass
    try:
        body = response.json()
        value = body.get("error", {}).get("details", {}).get("retryAfter")
        if value:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError, TypeError):
        pass
    return datetime.now(UTC) + timedelta(seconds=60)
