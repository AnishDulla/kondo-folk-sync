from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def _first_value(payload: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def _nested_value(payload: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        cursor: Any = payload
        for part in path:
            if not isinstance(cursor, dict) or part not in cursor:
                cursor = None
                break
            cursor = cursor[part]
        if cursor not in (None, ""):
            return cursor
    return None


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value).strip() or None


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("conversation_history"),
        payload.get("conversationHistory"),
        payload.get("messages"),
        payload.get("conversation"),
        _nested_value(payload, (("data", "messages"), ("chat", "messages"))),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [m for m in candidate if isinstance(m, dict)]
    return []


def _latest_message(payload: dict[str, Any], messages: list[dict[str, Any]]) -> str | None:
    direct = _stringify(
        _first_value(
            payload,
            (
                "latest_message",
                "latestMessage",
                "last_message",
                "lastMessage",
                "message",
                "text",
                "conversation_latest_content",
            ),
        )
    )
    if direct:
        return direct
    if messages:
        last = messages[-1]
        return _stringify(
            _first_value(last, ("text", "body", "content", "message", "messageText"))
        )
    return None


def _conversation_text(messages: list[dict[str, Any]], latest: str | None) -> str:
    if not messages:
        return latest or ""
    lines: list[str] = []
    for message in messages[-50:]:
        speaker = _stringify(
            _first_value(message, ("sender", "from", "author", "direction", "role"))
        )
        text = _stringify(
            _first_value(message, ("text", "body", "content", "message", "messageText"))
        )
        timestamp = _stringify(_first_value(message, ("timestamp", "createdAt", "date")))
        if not text:
            continue
        prefix = " ".join(part for part in (timestamp, speaker) if part)
        lines.append(f"{prefix}: {text}" if prefix else text)
    return "\n".join(lines)


def _canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip()
    cleaned = re.sub(r"\?.*$", "", cleaned)
    cleaned = cleaned.rstrip("/")
    return cleaned or None


@dataclass(frozen=True)
class NormalizedKondoEvent:
    linkedin_url: str | None
    full_name: str | None
    headline: str | None
    location: str | None
    kondo_labels: list[str]
    kondo_notes: str | None
    kondo_url: str | None
    latest_conversation_timestamp: str | None
    latest_message: str | None
    conversation_text: str
    raw_payload: dict[str, Any] = field(repr=False)

    @property
    def idempotency_key(self) -> str:
        fingerprint = {
            "linkedin_url": self.linkedin_url,
            "timestamp": self.latest_conversation_timestamp,
            "latest_message": self.latest_message,
        }
        encoded = json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def has_full_history(self) -> bool:
        return "\n" in self.conversation_text

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass(frozen=True)
class AIAnalysis:
    summary: str
    crm_note: str
    relationship_stage: str
    reply_owner: str
    next_action: str
    follow_up_date: str | None
    confidence: float
    meeting_detected: bool
    important_context: list[str]
    group_category: str
    group_reason: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AIAnalysis":
        group_category = str(data.get("group_category") or data.get("folk_group") or "").strip()
        if group_category not in {
            "claims_professionals",
            "distribution_partners",
            "tpas_subrogation_attorneys",
        }:
            group_category = "claims_professionals"
        return cls(
            summary=str(data.get("summary") or "").strip(),
            crm_note=str(data.get("crm_note") or data.get("summary") or "").strip(),
            relationship_stage=str(data.get("relationship_stage") or "active_conversation"),
            reply_owner=str(data.get("reply_owner") or "neutral"),
            next_action=str(data.get("next_action") or "Review the conversation."),
            follow_up_date=data.get("follow_up_date") or None,
            confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.0))),
            meeting_detected=bool(data.get("meeting_detected") or False),
            important_context=[
                str(item).strip()
                for item in data.get("important_context", [])
                if str(item).strip()
            ],
            group_category=group_category,
            group_reason=str(data.get("group_reason") or "").strip() or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_kondo_payload(payload: dict[str, Any]) -> NormalizedKondoEvent:
    source = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    messages = _extract_messages(payload)
    latest = _latest_message(payload, messages)
    if not latest and source is not payload:
        latest = _latest_message(source, messages)
    linkedin_url = _canonical_url(
        _stringify(
            _first_value(
                source,
                (
                    "linkedin_url",
                    "linkedinUrl",
                    "profileUrl",
                    "profile_url",
                    "contact_linkedin_url",
                ),
            )
            or _nested_value(source, (("person", "linkedinUrl"), ("contact", "linkedinUrl")))
        )
    )
    labels = _first_value(source, ("kondo_labels", "kondoLabels", "labels")) or []
    if isinstance(labels, str):
        labels = [part.strip() for part in labels.split(",") if part.strip()]
    if not isinstance(labels, list):
        labels = []
    timestamp = _stringify(
        _first_value(
            source,
            (
                "latest_conversation_timestamp",
                "latestConversationTimestamp",
                "latestMessageAt",
                "updatedAt",
                "timestamp",
                "conversation_latest_timestamp",
            ),
        )
    )
    if not timestamp:
        timestamp = datetime.now(UTC).isoformat()

    first_name = _stringify(_first_value(source, ("contact_first_name", "firstName", "first_name")))
    last_name = _stringify(_first_value(source, ("contact_last_name", "lastName", "last_name")))
    full_name = _stringify(
        _first_value(source, ("full_name", "fullName", "name"))
        or _nested_value(source, (("person", "name"), ("contact", "name")))
    )
    if not full_name:
        full_name = " ".join(part for part in (first_name, last_name) if part) or None

    return NormalizedKondoEvent(
        linkedin_url=linkedin_url,
        full_name=full_name,
        headline=_stringify(
            _first_value(
                source,
                ("linkedin_headline", "headline", "title", "jobTitle", "contact_headline"),
            )
            or _nested_value(source, (("person", "headline"), ("contact", "headline")))
        ),
        location=_stringify(
            _first_value(source, ("linkedin_location", "location", "contact_location"))
            or _nested_value(source, (("person", "location"), ("contact", "location")))
        ),
        kondo_labels=[str(label) for label in labels],
        kondo_notes=_stringify(_first_value(source, ("kondo_notes", "kondoNotes", "notes", "kondo_note"))),
        kondo_url=_stringify(_first_value(source, ("kondo_url", "kondoUrl", "chatUrl"))),
        latest_conversation_timestamp=timestamp,
        latest_message=latest,
        conversation_text=_conversation_text(messages, latest),
        raw_payload=payload,
    )
