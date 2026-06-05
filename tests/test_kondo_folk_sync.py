from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kondo_folk_sync.ai import AIAnalyzer
from kondo_folk_sync.config import Settings
from kondo_folk_sync.folk import FolkClient, FolkRateLimitError, _folk_datetime, _status_for_analysis
from kondo_folk_sync.models import AIAnalysis, normalize_kondo_payload
from kondo_folk_sync.service import _process_payload, _should_skip_existing, create_app
from kondo_folk_sync.store import SyncStore


def test_normalize_payload_and_idempotency_key() -> None:
    payload = {
        "linkedinUrl": "https://www.linkedin.com/in/jane-doe/?miniProfileUrn=123",
        "fullName": "Jane Doe",
        "headline": "VP Claims at Example Mutual",
        "labels": ["Lead"],
        "latestMessageAt": "2026-05-18T12:00:00Z",
        "latestMessage": "Can you send me more info?",
    }

    event = normalize_kondo_payload(payload)

    assert event.linkedin_url == "https://www.linkedin.com/in/jane-doe"
    assert event.full_name == "Jane Doe"
    assert event.latest_message == "Can you send me more info?"
    assert event.idempotency_key == normalize_kondo_payload(payload).idempotency_key


def test_normalize_real_kondo_webhook_payload_shape() -> None:
    payload = {
        "data": {
            "contact_first_name": "Jessica",
            "contact_last_name": "Silva",
            "contact_headline": "Complex Subrogation Adjuster",
            "contact_linkedin_url": "https://www.linkedin.com/in/jessica-silva-6951b1b7",
            "contact_company": "Example Carrier",
            "conversation_latest_content": "Hi Jessica, wanted to share what we're building.",
            "conversation_latest_timestamp": "2026-05-08T20:58:15.902Z",
            "kondo_url": "https://app.trykondo.com/inboxes/all/example",
            "kondo_note": "",
            "kondo_labels": [{"kondo_label_id": "other", "kondo_label_name": "Other"}],
        },
        "event": {"type": "manual-update"},
    }

    event = normalize_kondo_payload(payload)

    assert event.linkedin_url == "https://www.linkedin.com/in/jessica-silva-6951b1b7"
    assert event.full_name == "Jessica Silva"
    assert event.headline == "Complex Subrogation Adjuster"
    assert event.company == "Example Carrier"
    assert event.kondo_labels == ["Other"]
    assert event.latest_message == "Hi Jessica, wanted to share what we're building."


def test_store_skips_duplicate_mapping(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    store.set_person_id("https://linkedin.com/in/a", "per_123")

    assert store.get_person_id("https://linkedin.com/in/a") == "per_123"


def test_heuristic_ai_creates_followup() -> None:
    settings = Settings(ai_provider="heuristic")
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "headline": "Claims Director at Example Mutual",
            "latestMessage": "Can you send me the details?",
        }
    )

    analysis = asyncio.run(AIAnalyzer(settings).analyze(event))

    assert analysis.reply_owner == "user_owes_reply"
    assert analysis.follow_up_date is not None
    assert analysis.confidence >= 0.5


def test_heuristic_ai_excludes_recruiters() -> None:
    settings = Settings(ai_provider="heuristic")
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/recruiter",
            "fullName": "Recruiter",
            "headline": "Technical Recruiter",
            "latestMessage": "I have a job opportunity that could be a fit.",
        }
    )

    analysis = asyncio.run(AIAnalyzer(settings).analyze(event))

    assert analysis.relationship_stage == "not_relevant"
    assert analysis.follow_up_date is None


def test_heuristic_ai_excludes_generic_consultants_without_insurance_context() -> None:
    settings = Settings(ai_provider="heuristic")
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/generic-consultant",
            "fullName": "Generic Consultant",
            "headline": "Independent Consultant",
            "company": "General Advisory LLC",
            "latestMessage": "Happy to connect and compare notes.",
        }
    )

    analysis = asyncio.run(AIAnalyzer(settings).analyze(event))

    assert analysis.relationship_stage == "not_relevant"
    assert analysis.follow_up_date is None


def test_heuristic_ai_keeps_insurance_claims_consultants() -> None:
    settings = Settings(ai_provider="heuristic")
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/claims-consultant",
            "fullName": "Claims Consultant",
            "headline": "CPCU, Consultant and Semi-Retired Insurance Claims Executive",
            "company": "Claims Advisory LLC",
            "latestMessage": "AI in subrogation sounds interesting.",
        }
    )

    analysis = asyncio.run(AIAnalyzer(settings).analyze(event))

    assert analysis.relationship_stage != "not_relevant"
    assert analysis.group_category in {"claims_professionals", "distribution_partners"}


def test_ai_analyzer_loads_prompt_from_file(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Custom CRM prompt", encoding="utf-8")
    settings = Settings(ai_provider="heuristic", prompt_path=prompt_path)

    analyzer = AIAnalyzer(settings)

    assert analyzer.system_prompt == "Custom CRM prompt"


def test_dry_run_plans_folk_writes(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        folk_group_id=None,
        folk_claims_professionals_group_id=None,
        folk_distribution_partners_group_id=None,
        folk_tpas_subrogation_attorneys_group_id=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "headline": "Claims Director at Example Mutual",
            "latestMessage": "Can you send me the details?",
        }
    )
    analysis = asyncio.run(AIAnalyzer(settings).analyze(event))

    result = asyncio.run(FolkClient(settings, store).sync(event, analysis))

    assert result["status"] == "dry_run"
    assert result["would_write"]["person"]["fullName"] == "Prospect"
    assert "groups" not in result["would_write"]["person"]
    assert result["would_write"]["reminder"] is not None


def test_person_payload_adds_group_only_when_configured(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        folk_group_id="grp_123",
        folk_claims_professionals_group_id=None,
        folk_distribution_partners_group_id=None,
        folk_tpas_subrogation_attorneys_group_id=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
        }
    )

    analysis = AIAnalysis.from_dict(
        {
            "summary": "Summary",
            "crm_note": "Durable CRM note",
            "group_category": "claims_professionals",
        }
    )
    payload = FolkClient(settings, store)._person_payload(event, analysis)

    assert payload["groups"] == [{"id": "grp_123"}]


def test_person_payload_uses_ai_selected_group(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        folk_group_id="grp_fallback",
        folk_claims_professionals_group_id="grp_claims",
        folk_distribution_partners_group_id="grp_distribution",
        folk_tpas_subrogation_attorneys_group_id="grp_tpa",
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
        }
    )
    analysis = AIAnalysis.from_dict(
        {
            "summary": "Summary",
            "crm_note": "Durable CRM note",
            "group_category": "distribution_partners",
            "group_reason": "Partner profile.",
        }
    )

    payload = FolkClient(settings, store)._person_payload(event, analysis)

    assert payload["groups"] == [{"id": "grp_distribution"}]
    assert payload["customFieldValues"] == {"grp_distribution": {"Status": "Lead"}}


def test_status_mapping_for_folk_pipeline() -> None:
    assert _status_for_analysis(
        AIAnalysis.from_dict({"summary": "s", "relationship_stage": "closed_lost"})
    ) == "Closed-lost"
    assert _status_for_analysis(
        AIAnalysis.from_dict(
            {
                "summary": "s",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
            }
        )
    ) == "Follow-up"
    assert _status_for_analysis(
        AIAnalysis.from_dict({"summary": "s", "relationship_stage": "meeting_booked"})
    ) == "Qualified"


def test_folk_datetime_uses_utc_shape() -> None:
    assert _folk_datetime("2026-05-18T12:00:00Z") == "2026-05-18T12:00:00.000Z"


def test_kondo_webhook_accepts_x_api_key(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        webhook_secret="secret",
    )
    client = TestClient(create_app(settings))
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "headline": "Claims Director at Example Mutual",
        "latestMessage": "Can you send me the details?",
    }

    rejected = client.post("/webhooks/kondo", json=payload)
    accepted = client.post("/webhooks/kondo", json=payload, headers={"x-api-key": "secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "queued"


def test_dry_run_events_do_not_block_later_live_processing() -> None:
    assert _should_skip_existing("dry_run", "https://linkedin.com/in/prospect", dry_run=True)
    assert not _should_skip_existing("dry_run", "https://linkedin.com/in/prospect", dry_run=False)
    assert _should_skip_existing("synced", "https://linkedin.com/in/prospect", dry_run=False)
    assert _should_skip_existing("excluded", "https://linkedin.com/in/recruiter", dry_run=False)


def test_not_relevant_analysis_does_not_write_to_folk(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=False,
        ai_provider="heuristic",
        folk_api_key="unused-because-excluded",
        admin_token="admin-secret",
    )
    client = TestClient(create_app(settings))
    payload = {
        "linkedinUrl": "https://linkedin.com/in/recruiter",
        "fullName": "Recruiter",
        "headline": "Technical Recruiter",
        "latestMessage": "I have a job opportunity that could be a fit.",
    }

    queued = client.post("/sync/manual", json=payload)
    response = client.post("/admin/process", headers={"x-admin-token": "admin-secret"})

    assert queued.json()["status"] == "queued"
    assert response.status_code == 200
    assert response.json()["attempted"][0]["status"] == "excluded"


def test_review_mode_stages_then_sends_batch_to_folk(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    client = TestClient(create_app(settings))
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "headline": "Claims Director at Example Mutual",
        "latestMessage": "Can you send me the details?",
    }

    queued = client.post("/sync/manual", json=payload)
    idempotency_key = queued.json()["idempotency_key"]
    analyzed = client.post("/admin/process", headers={"x-admin-token": "admin-secret"})
    staged = client.post(
        f"/admin/stage/{idempotency_key}",
        headers={"x-admin-token": "admin-secret"},
    )
    not_pushed = client.post("/admin/process", headers={"x-admin-token": "admin-secret"})
    sent = client.post("/admin/send-staged", headers={"x-admin-token": "admin-secret"})
    pushed = client.post("/admin/process", headers={"x-admin-token": "admin-secret"})

    assert analyzed.json()["attempted"][0]["status"] == "review_pending"
    assert staged.json()["status"] == "staged_for_folk"
    assert not_pushed.json()["count"] == 0
    assert sent.json()["status"] == "queued_for_folk"
    assert sent.json()["count"] == 1
    assert pushed.json()["attempted"][0]["status"] == "dry_run"


def test_stage_selected_can_stage_already_synced_rows(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    store = SyncStore(settings.database_path)
    first = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/synced-prospect",
            "fullName": "Synced Prospect",
            "latestMessage": "Already pushed.",
        }
    )
    second = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/pending-prospect",
            "fullName": "Pending Prospect",
            "latestMessage": "Pending review.",
        }
    )
    analysis = AIAnalysis.from_dict(
        {
            "summary": "Prospect update.",
            "relationship_stage": "active_conversation",
            "reply_owner": "neutral",
            "next_action": "Review.",
            "confidence": 0.8,
            "group_category": "claims_professionals",
        }
    ).to_dict()
    store.start_event(first.idempotency_key, first.linkedin_url, first.to_dict())
    store.finish_event(first.idempotency_key, "synced", analysis=analysis)
    store.start_event(second.idempotency_key, second.linkedin_url, second.to_dict())
    store.finish_event(second.idempotency_key, "review_pending", analysis=analysis)
    client = TestClient(create_app(settings))

    response = client.post(
        "/admin/stage-selected",
        data={"selected": [first.idempotency_key, second.idempotency_key]},
        headers={"x-admin-token": "admin-secret"},
    )

    assert response.status_code == 200
    assert response.json()["count"] == 2
    assert SyncStore(settings.database_path).get_event(first.idempotency_key)["status"] == "staged_for_folk"
    assert SyncStore(settings.database_path).get_event(second.idempotency_key)["status"] == "staged_for_folk"


def test_unstage_restores_previous_status(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/synced-prospect",
            "fullName": "Synced Prospect",
            "latestMessage": "Already pushed.",
        }
    )
    analysis = AIAnalysis.from_dict(
        {
            "summary": "Prospect update.",
            "relationship_stage": "active_conversation",
            "reply_owner": "neutral",
            "next_action": "Review.",
            "confidence": 0.8,
            "group_category": "claims_professionals",
        }
    ).to_dict()
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(event.idempotency_key, "synced", analysis=analysis)
    store.stage_for_folk(event.idempotency_key)
    client = TestClient(create_app(settings))

    response = client.post(
        f"/admin/unstage/{event.idempotency_key}",
        headers={"x-admin-token": "admin-secret"},
    )

    assert response.status_code == 200
    assert SyncStore(settings.database_path).get_event(event.idempotency_key)["status"] == "synced"


def test_console_actions_redirect_back_to_console(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    client = TestClient(create_app(settings))
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "headline": "Claims Director at Example Mutual",
        "latestMessage": "Can you send me the details?",
    }
    queued = client.post("/sync/manual", json=payload)
    idempotency_key = queued.json()["idempotency_key"]
    client.post("/admin/process", headers={"x-admin-token": "admin-secret"})

    response = client.post(
        f"/admin/stage/{idempotency_key}?token=admin-secret",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/console?token=admin-secret&notice=")


def test_request_full_sync_marks_review_item(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    client = TestClient(create_app(settings))
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "headline": "Claims Director at Example Mutual",
        "latestMessage": "Can you send me the details?",
    }

    queued = client.post("/sync/manual", json=payload)
    idempotency_key = queued.json()["idempotency_key"]
    client.post("/admin/process", headers={"x-admin-token": "admin-secret"})
    response = client.post(
        f"/admin/request-full-sync/{idempotency_key}",
        headers={"x-admin-token": "admin-secret"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "full_sync_requested"
    assert SyncStore(settings.database_path).get_event(idempotency_key)["status"] == "full_sync_requested"


def test_reset_local_state_requires_confirmation(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=True,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "headline": "Claims Director at Example Mutual",
            "latestMessage": "Can you send me the details?",
        }
    )
    store.queue_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.set_person_id(event.linkedin_url or "", "per_123")
    client = TestClient(create_app(settings))

    rejected = client.post(
        "/admin/reset-local-state",
        data={"confirm": "wrong"},
        headers={"x-admin-token": "admin-secret"},
    )
    accepted = client.post(
        "/admin/reset-local-state",
        data={"confirm": "RESET"},
        headers={"x-admin-token": "admin-secret"},
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert SyncStore(settings.database_path).get_event(event.idempotency_key) is None
    assert SyncStore(settings.database_path).get_person_id(event.linkedin_url or "") is None


def test_admin_stats_requires_token_in_live_mode(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=False,
        ai_provider="heuristic",
        admin_token="admin-secret",
    )
    client = TestClient(create_app(settings))

    rejected = client.get("/admin/stats")
    accepted = client.get("/admin/stats", headers={"x-admin-token": "admin-secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["health"]["dry_run"] is False


def test_console_available_in_local_dry_run_without_token(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token=None,
    )
    client = TestClient(create_app(settings))

    response = client.get("/console")

    assert response.status_code == 200
    assert "Kondo folk Sync Console" in response.text


def test_store_priority_events_rank_actionable_contacts(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    hot_event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/hot-prospect",
            "fullName": "Hot Prospect",
            "latestMessage": "Tuesday works for a call.",
            "kondoUrl": "https://app.trykondo.com/inboxes/all/hot",
        }
    )
    warm_event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/warm-prospect",
            "fullName": "Warm Prospect",
            "latestMessage": "Thanks.",
            "kondoUrl": "https://app.trykondo.com/inboxes/all/warm",
        }
    )
    store.start_event(hot_event.idempotency_key, hot_event.linkedin_url, hot_event.to_dict())
    store.finish_event(
        hot_event.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Call is likely.",
                "relationship_stage": "meeting_booked",
                "reply_owner": "user_owes_reply",
                "next_action": "Send calendar invite.",
                "follow_up_date": "2026-06-03",
                "meeting_detected": True,
                "confidence": 0.91,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    store.start_event(warm_event.idempotency_key, warm_event.linkedin_url, warm_event.to_dict())
    store.finish_event(
        warm_event.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Light response.",
                "relationship_stage": "active_conversation",
                "reply_owner": "neutral",
                "next_action": "Monitor.",
                "confidence": 0.51,
                "group_category": "distribution_partners",
            }
        ).to_dict(),
    )

    priority = store.priority_events(limit=10)

    assert priority[0]["full_name"] == "Hot Prospect"
    assert priority[0]["kondo_url"] == "https://app.trykondo.com/inboxes/all/hot"
    assert priority[0]["needs_full_history"] is True
    assert priority[0]["score"] > priority[1]["score"]


def test_store_triage_events_include_low_priority_contacts(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    actionable = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/actionable",
            "fullName": "Actionable Prospect",
            "latestMessage": "Can you send me details?",
            "latestMessageAt": "2026-06-02T12:00:00Z",
        }
    )
    excluded = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/recruiter",
            "fullName": "Recruiter",
            "headline": "Technical Recruiter",
            "latestMessage": "I have a job opportunity.",
            "latestMessageAt": "2026-06-02T13:00:00Z",
        }
    )
    store.start_event(actionable.idempotency_key, actionable.linkedin_url, actionable.to_dict())
    store.finish_event(
        actionable.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    store.start_event(excluded.idempotency_key, excluded.linkedin_url, excluded.to_dict())
    store.finish_event(
        excluded.idempotency_key,
        "excluded",
        analysis={
            "summary": "Recruiter outreach.",
            "crm_note": "Recruiter outreach.",
            "relationship_stage": "not_relevant",
            "reply_owner": "neutral",
            "next_action": "No CRM action.",
            "follow_up_date": None,
            "confidence": 0.9,
            "meeting_detected": False,
            "important_context": [],
            "group_category": "claims_professionals",
            "group_reason": "Recruiting message.",
        },
    )

    triage = store.triage_events(limit=10)

    assert [item["full_name"] for item in triage] == ["Recruiter", "Actionable Prospect"]
    assert triage[0]["status"] == "excluded"
    assert triage[0]["score"] == 0
    assert triage[1]["sync_depth"] == "latest_message"
    assert triage[1]["has_full_history"] is False
    assert triage[1]["score"] > triage[0]["score"]


def test_store_triage_marks_full_history_payloads(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/full-history",
            "fullName": "Full History Prospect",
            "messages": [
                {"sender": "me", "text": "Initial outreach"},
                {"sender": "prospect", "text": "Can you send details?"},
            ],
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "review_pending",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )

    triage = store.triage_events(limit=10)

    assert triage[0]["sync_depth"] == "full_history"
    assert triage[0]["has_full_history"] is True


def test_kondo_string_conversation_history_marks_full_history(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    event = normalize_kondo_payload(
        {
            "data": {
                "contact_linkedin_url": "https://www.linkedin.com/in/full-history-string",
                "contact_first_name": "Full",
                "contact_last_name": "String",
                "contact_headline": "Claims Director",
                "conversation_history": "Me: Initial outreach\nProspect: Send me details.",
                "conversation_latest_content": "Send me details.",
                "conversation_latest_timestamp": "2026-06-02T12:00:00Z",
            }
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "review_pending",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )

    triage = store.triage_events(limit=10)

    assert event.has_full_history is True
    assert "Initial outreach" in event.conversation_text
    assert triage[0]["sync_depth"] == "full_history"


def test_new_message_for_existing_person_creates_new_review_item(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    first = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/repeat-prospect",
            "fullName": "Repeat Prospect",
            "latestMessage": "First message",
            "latestMessageAt": "2026-06-01T18:00:00Z",
        }
    )
    second = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/repeat-prospect",
            "fullName": "Repeat Prospect",
            "latestMessage": "New reply next day",
            "latestMessageAt": "2026-06-02T18:00:00Z",
        }
    )
    analysis = AIAnalysis.from_dict(
        {
            "summary": "Prospect replied.",
            "relationship_stage": "active_conversation",
            "reply_owner": "user_owes_reply",
            "next_action": "Respond.",
            "confidence": 0.85,
            "group_category": "claims_professionals",
        }
    ).to_dict()
    store.start_event(first.idempotency_key, first.linkedin_url, first.to_dict())
    store.finish_event(first.idempotency_key, "synced", analysis=analysis)
    store.start_event(second.idempotency_key, second.linkedin_url, second.to_dict())
    store.finish_event(second.idempotency_key, "review_pending", analysis=analysis)

    triage = store.triage_events(limit=10)

    assert first.idempotency_key != second.idempotency_key
    assert triage[0]["idempotency_key"] == second.idempotency_key
    assert triage[0]["status"] == "review_pending"


def test_admin_priority_requires_token_and_returns_items(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=False,
        ai_provider="heuristic",
        admin_token="admin-secret",
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "latestMessage": "Can you send me details?",
            "kondoUrl": "https://app.trykondo.com/inboxes/all/prospect",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    client = TestClient(create_app(settings))

    rejected = client.get("/admin/priority")
    accepted = client.get("/admin/priority", headers={"x-admin-token": "admin-secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["items"][0]["full_name"] == "Prospect"


def test_admin_triage_requires_token_and_returns_items(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=False,
        ai_provider="heuristic",
        admin_token="admin-secret",
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "latestMessage": "Can you send me details?",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    client = TestClient(create_app(settings))

    rejected = client.get("/admin/triage")
    accepted = client.get("/admin/triage", headers={"x-admin-token": "admin-secret"})

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["items"][0]["full_name"] == "Prospect"


def test_console_shows_daily_triage_contacts(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "latestMessage": "Can you send me details?",
            "kondoUrl": "https://app.trykondo.com/inboxes/all/prospect",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "review_pending",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    client = TestClient(create_app(settings))

    response = client.get("/console")

    assert response.status_code == 200
    assert "Daily Triage" in response.text
    assert "Prospect" in response.text
    assert "Selected Batch" in response.text
    assert "No contacts are in the selected batch yet." in response.text
    assert "AI Readout" in response.text
    assert "Not selected" in response.text
    assert "Latest message" in response.text
    assert "Select Checked" in response.text
    assert "Select Latest Recap" in response.text
    assert "Get Full History First" in response.text
    assert "Send Selected Batch to folk" in response.text
    assert "Advanced queue tools" in response.text
    assert "Process Queue" not in response.text


def test_console_allows_repush_for_synced_rows(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "latestMessage": "Can you send me details?",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "synced",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "active_conversation",
                "reply_owner": "neutral",
                "next_action": "Review.",
                "confidence": 0.8,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    client = TestClient(create_app(settings))

    response = client.get("/console")

    assert response.status_code == 200
    assert "Select to Resend Latest message" in response.text
    assert "Sent to folk" in response.text
    assert "name='selected'" in response.text


def test_console_shows_full_history_ready_for_selected_rows(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/full-history",
            "fullName": "Full History Prospect",
            "headline": "Claims Director",
            "conversation_history": "Me: Initial outreach\nProspect: Send me details.",
            "latestMessage": "Send me details.",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "review_pending",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "needs_follow_up",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    store.stage_for_folk(event.idempotency_key)
    client = TestClient(create_app(settings))

    response = client.get("/console")

    assert response.status_code == 200
    assert "Full history ready" in response.text
    assert "selected: full history" in response.text
    assert "Select Full History" not in response.text


def test_console_shows_latest_only_selected_warning(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token=None,
    )
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/latest-only",
            "fullName": "Latest Only Prospect",
            "headline": "Claims Director",
            "latestMessage": "Can you send me the details?",
        }
    )
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(
        event.idempotency_key,
        "review_pending",
        analysis=AIAnalysis.from_dict(
            {
                "summary": "Prospect asked for details.",
                "relationship_stage": "needs_follow_up",
                "reply_owner": "user_owes_reply",
                "next_action": "Send details.",
                "confidence": 0.85,
                "group_category": "claims_professionals",
            }
        ).to_dict(),
    )
    store.stage_for_folk(event.idempotency_key)
    client = TestClient(create_app(settings))

    response = client.get("/console")

    assert response.status_code == 200
    assert "Latest only - full history recommended" in response.text
    assert "selected: latest only" in response.text
    assert "Get Full History First" in response.text


def test_admin_reprocess_replays_stored_payload(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=False,
    )
    client = TestClient(create_app(settings))
    payload = {
                "linkedinUrl": "https://linkedin.com/in/prospect",
                "fullName": "Prospect",
                "headline": "Claims Director at Example Mutual",
                "latestMessage": "Can you send me the details?",
            }

    first = client.post("/sync/manual", json=payload)
    idempotency_key = first.json()["idempotency_key"]
    replay = client.post(
        f"/admin/reprocess/{idempotency_key}",
        headers={"x-admin-token": "admin-secret"},
    )

    assert replay.status_code == 200
    assert replay.json()["idempotency_key"] == idempotency_key
    assert replay.json()["status"] == "queued"


def test_admin_reconcile_retries_error_events(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "sync.db",
        dry_run=True,
        ai_provider="heuristic",
        admin_token="admin-secret",
        review_mode=False,
    )
    store = SyncStore(settings.database_path)
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "headline": "Claims Director at Example Mutual",
        "latestMessage": "Can you send me the details?",
    }
    event = normalize_kondo_payload(payload)
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())
    store.finish_event(event.idempotency_key, "error", error="temporary failure")

    client = TestClient(create_app(settings))
    response = client.post("/admin/reconcile", headers={"x-admin-token": "admin-secret"})

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert store.get_event(event.idempotency_key)["status"] == "dry_run"


def test_stale_processing_events_are_queue_depth(tmp_path: Path) -> None:
    store = SyncStore(tmp_path / "sync.db")
    payload = {
        "linkedinUrl": "https://linkedin.com/in/prospect",
        "fullName": "Prospect",
        "latestMessage": "Can you send me the details?",
    }
    event = normalize_kondo_payload(payload)
    store.start_event(event.idempotency_key, event.linkedin_url, event.to_dict())

    assert store.queue_depth(processing_timeout_seconds=0) == 1
    assert store.next_queued_event(processing_timeout_seconds=0)["idempotency_key"] == event.idempotency_key


def test_rate_limit_defers_event(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "sync.db", ai_provider="heuristic", review_mode=False)
    store = SyncStore(settings.database_path)
    event = normalize_kondo_payload(
        {
            "linkedinUrl": "https://linkedin.com/in/prospect",
            "fullName": "Prospect",
            "headline": "Claims Director at Example Mutual",
            "latestMessage": "Can you send me the details?",
        }
    )

    class RateLimitedFolk:
        def __init__(self, folk_settings):
            self.settings = folk_settings

        async def sync(self, _event, _analysis):
            raise FolkRateLimitError(
                "folk rate limit reached",
                datetime.now(UTC) + timedelta(seconds=30),
            )

    result = asyncio.run(_process_payload(event.to_dict(), store, AIAnalyzer(settings), RateLimitedFolk(settings)))

    assert result["result"]["status"] == "retry_wait"
    assert store.get_event(event.idempotency_key)["status"] == "retry_wait"
