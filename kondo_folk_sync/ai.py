from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import Settings
from .models import AIAnalysis, NormalizedKondoEvent


DEFAULT_SYSTEM_PROMPT = """You are an AI CRM operations layer between Kondo and folk.
Analyze LinkedIn Sales Navigator conversation data for outbound follow-up.
Return only valid JSON with these keys:
summary, crm_note, relationship_stage, reply_owner, next_action, follow_up_date,
confidence, meeting_detected, important_context, group_category, group_reason.

Allowed reply_owner values: user_owes_reply, prospect_owes_reply, neutral.
Allowed group_category values: claims_professionals, distribution_partners,
tpas_subrogation_attorneys.
Use YYYY-MM-DD for follow_up_date or null. Be conservative with reminders."""


def _json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        raise ValueError("AI response did not contain JSON")
    return json.loads(match.group(0))


def _event_prompt(event: NormalizedKondoEvent) -> str:
    return json.dumps(
        {
            "linkedin_url": event.linkedin_url,
            "full_name": event.full_name,
            "headline": event.headline,
            "company": event.company,
            "location": event.location,
            "kondo_labels": event.kondo_labels,
            "kondo_notes": event.kondo_notes,
            "latest_conversation_timestamp": event.latest_conversation_timestamp,
            "latest_message": event.latest_message,
            "conversation_text": event.conversation_text,
        },
        indent=2,
        default=str,
    )


class AIAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        try:
            prompt = self.settings.prompt_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return DEFAULT_SYSTEM_PROMPT
        return prompt or DEFAULT_SYSTEM_PROMPT

    async def analyze(self, event: NormalizedKondoEvent) -> AIAnalysis:
        provider = self.settings.ai_provider.lower()
        if provider == "auto":
            if self.settings.openai_api_key:
                provider = "openai"
            elif self.settings.anthropic_api_key:
                provider = "anthropic"
            else:
                provider = "heuristic"

        if provider == "openai":
            return await self._openai(event)
        if provider == "anthropic":
            return await self._anthropic(event)
        return self._heuristic(event)

    async def _openai(self, event: NormalizedKondoEvent) -> AIAnalysis:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI analysis")
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.settings.openai_model,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": _event_prompt(event)},
                    ],
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return AIAnalysis.from_dict(_json_from_text(content))

    async def _anthropic(self, event: NormalizedKondoEvent) -> AIAnalysis:
        if not self.settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for Anthropic analysis")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic package is required for Anthropic analysis") from exc

        client = anthropic.AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        response = await client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=1200,
            temperature=0.1,
            system=self.system_prompt,
            messages=[{"role": "user", "content": _event_prompt(event)}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        return AIAnalysis.from_dict(_json_from_text(text))

    def _heuristic(self, event: NormalizedKondoEvent) -> AIAnalysis:
        text = f"{event.conversation_text}\n{event.latest_message or ''}".lower()
        meeting_detected = any(
            word in text
            for word in ("meeting", "call", "zoom", "calendar", "calendly", "demo", "chat")
        )
        if _looks_non_prospect(event) or not _looks_relevant_to_recourse(event):
            return AIAnalysis(
                summary=event.latest_message or event.conversation_text or "Non-prospecting conversation.",
                crm_note=event.latest_message or event.conversation_text or "Non-prospecting conversation.",
                relationship_stage="not_relevant",
                reply_owner="neutral",
                next_action="Do not sync this conversation into the prospecting CRM workflow.",
                follow_up_date=None,
                confidence=0.65,
                meeting_detected=meeting_detected,
                important_context=["Excluded from prospecting sync by heuristic filter."],
                group_category="distribution_partners",
                group_reason="Conversation appears to be recruiter, personal, or otherwise unrelated to Recourse prospecting.",
            )
        prospect_asked = "?" in (event.latest_message or "") and not _looks_like_user_message(event.latest_message)
        user_owes = prospect_asked or any(
            phrase in text
            for phrase in (
                "can you send",
                "send me",
                "what times",
                "interested",
                "let's talk",
                "lets talk",
            )
        )
        if user_owes:
            reply_owner = "user_owes_reply"
            follow_up_date = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
            next_action = "Reply on LinkedIn and move the conversation to the next concrete step."
            stage = "needs_follow_up"
        else:
            reply_owner = "prospect_owes_reply" if event.latest_message else "neutral"
            follow_up_date = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
            next_action = "Follow up if there is no reply by the reminder date."
            stage = "active_conversation"

        summary_source = event.conversation_text or event.latest_message or "Kondo conversation synced."
        summary = " ".join(summary_source.split())[:700]
        context = []
        if event.kondo_notes:
            context.append(f"Kondo note: {event.kondo_notes}")
        if event.kondo_labels:
            context.append(f"Kondo labels: {', '.join(event.kondo_labels)}")
        if meeting_detected:
            context.append("Conversation appears to mention a meeting or call.")

        return AIAnalysis(
            summary=summary,
            crm_note=summary,
            relationship_stage=stage,
            reply_owner=reply_owner,
            next_action=next_action,
            follow_up_date=follow_up_date,
            confidence=0.55,
            meeting_detected=meeting_detected,
            important_context=context,
            group_category=_heuristic_group_category(event),
            group_reason="Heuristic classification from LinkedIn headline and message text.",
        )


def _looks_like_user_message(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return lowered.startswith(("i ", "i'm ", "we ", "we're ", "thanks", "great"))


def _heuristic_group_category(event: NormalizedKondoEvent) -> str:
    text = " ".join(
        part.lower()
        for part in (event.full_name, event.headline, event.company, event.latest_message, event.conversation_text)
        if part
    )
    if any(term in text for term in ("attorney", "law", "counsel", "subrogation counsel", "tpa")):
        return "tpas_subrogation_attorneys"
    if any(term in text for term in ("partner", "consultant", "advisor", "broker", "sales", "go to market", "gtm")):
        return "distribution_partners"
    return "claims_professionals"


def _looks_non_prospect(event: NormalizedKondoEvent) -> bool:
    text = " ".join(
        part.lower()
        for part in (event.full_name, event.headline, event.company, event.latest_message, event.conversation_text)
        if part
    )
    prospect_terms = (
        "claim",
        "claims",
        "carrier",
        "insurer",
        "insurance",
        "subrogation",
        "recovery",
        "siu",
        "tpa",
        "attorney",
        "counsel",
        "broker",
        "consultant",
        "partner",
        "gtm",
        "recourse",
    )
    non_prospect_terms = (
        "recruiter",
        "recruiting",
        "talent acquisition",
        "hiring",
        "job opportunity",
        "open role",
        "career opportunity",
        "resume",
        "candidate",
        "staffing",
        "sourcing",
        "personal trainer",
        "dating",
        "friend",
        "family",
    )
    return any(term in text for term in non_prospect_terms) and not any(
        term in text for term in prospect_terms
    )


def _looks_relevant_to_recourse(event: NormalizedKondoEvent) -> bool:
    text = " ".join(
        part.lower()
        for part in (
            event.full_name,
            event.headline,
            event.company,
            event.latest_message,
            event.conversation_text,
            " ".join(event.kondo_labels),
            event.kondo_notes,
        )
        if part
    )
    strong_terms = (
        "claim",
        "claims",
        "carrier",
        "insurer",
        "insurance",
        "p&c",
        "property and casualty",
        "subrogation",
        "recovery",
        "siu",
        "tpa",
        "third-party administrator",
        "attorney",
        "lawyer",
        "counsel",
        "litigation",
        "broker",
        "underwriting",
        "adjuster",
        "cpcu",
        "aic",
        "claims executive",
        "insurance tech",
        "insurtech",
        "recourse",
    )
    partner_terms = (
        "distribution partner",
        "referral partner",
        "go to market",
        "gtm",
        "advisor",
        "consultant",
        "consulting",
    )
    if any(term in text for term in strong_terms):
        return True
    return any(term in text for term in partner_terms) and any(
        term in text
        for term in (
            "insurance",
            "carrier",
            "claims",
            "broker",
            "subrogation",
            "recovery",
            "p&c",
            "insurtech",
        )
    )
