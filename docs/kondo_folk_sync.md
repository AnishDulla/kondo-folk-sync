# Kondo to folk AI Sync

This service receives Kondo LinkedIn/Sales Navigator webhook payloads, runs an AI
classification step, and writes clean CRM updates into folk.

## Control Surface

For v1, the user-facing control surface is Kondo plus a small sync console.

The default always-on mode should be Kondo's streaming webhook:

- Kondo posts to this service whenever a conversation changes.
- The service queues each event quickly so Kondo is not blocked by folk API
  limits.
- A slow worker analyzes one contact at a time.
- In review mode, the analyzed contact waits in the console until you approve
  it. Outside review mode, folk is updated immediately with the current CRM
  note, status, group, interaction, and follow-up reminder when needed.

For deeper, on-demand refreshes, use Kondo's existing manual sync:

- Open a conversation in Kondo.
- Click the lightning sync button or run `Cmd+K > Sync`.
- Kondo posts the conversation payload to this service.
- The AI layer summarizes the thread and writes to folk.

Kondo's manual sync from inside a specific chat sends richer conversation
history than streaming events, so use it when a relationship needs a complete
CRM refresh.

The repo also exposes `POST /sync/manual` for local testing with a captured
payload, but daily usage should start from Kondo.

The service console lives at:

```text
/console?token=<KONDO_FOLK_ADMIN_TOKEN>
```

Use it to review analyzed contacts, choose which recaps are ready for folk, and
send the selected batch. In production, this is the daily control surface: open
the hosted console after Kondo has sent new events, review the daily table, and
click `Send Selected Batch to folk` only when the batch looks right.

The console also has a `Daily Triage` table. This is the main workflow view for
the day. It shows recent analyzed contacts in the same rich format, sorted from
newest Kondo conversation to oldest. The score is an attention signal inside
the row, not the primary sort:

- contacts where the AI thinks you owe a reply
- meetings or meeting-like signals
- contacts with a follow-up date
- active conversations with enough confidence to warrant review
- excluded contacts, so personal/recruiting/noise decisions remain visible

In review mode, this table is the end-of-day queue:

- Each row shows a sync-depth tag: `Latest message` means folk will receive only
  the latest-message recap; `Full history` means Kondo has sent a richer
  conversation-history payload and that deeper recap is what will be pushed.
- `Select Latest Recap` or `Select Full History` puts that exact row payload in
  the selected end-of-day batch. Selecting does not push to folk.
- You can also select already-pushed rows and stage them again when you want to
  repush/recalibrate the folk record from the latest analysis.
- `Get Full History First` marks the row as requiring deeper conversation
  history.
- `Open Kondo Full Sync` opens the Kondo thread so you can run Kondo's manual
  sync inside that conversation.
- `Select All Unreviewed` selects all remaining latest-message rows after you have
  marked the deeper-sync cases.
- `Select Checked` selects only the checked rows.
- `Send Selected Batch to folk` is the only batch button that queues selected rows
  for folk writes.
- Console button clicks return to the console with a status notice. The API JSON
  endpoints remain available for scripts.

Once a row is sent, the worker pushes it to folk slowly and safely. If a
full-history Kondo payload arrives after manual sync, it is analyzed and shown
for review before being pushed.

Queue maintenance actions such as processing incoming queue items, retrying
failed work, and JSON stats live under `Advanced queue tools`; they are not part
of the normal daily review flow.

## Setup

Create a local `.env` with:

```bash
FOLK_API_KEY=...
FOLK_GROUP_ID=...
FOLK_GROUP_CLAIMS_PROFESSIONALS_ID=
FOLK_GROUP_DISTRIBUTION_PARTNERS_ID=
FOLK_GROUP_TPAS_SUBROGATION_ATTORNEYS_ID=
KONDO_FOLK_DRY_RUN=true
KONDO_FOLK_AI_PROVIDER=auto
OPENAI_API_KEY=...
# or ANTHROPIC_API_KEY=...
KONDO_WEBHOOK_SECRET=choose-a-shared-secret
KONDO_FOLK_ADMIN_TOKEN=choose-a-different-admin-secret
KONDO_FOLK_DB=./kondo_folk_sync.db
KONDO_FOLK_PROMPT_PATH=kondo_folk_sync/prompts/crm_analysis.md
KONDO_FOLK_RECONCILE_INTERVAL_MINUTES=0
KONDO_FOLK_REVIEW_MODE=true
KONDO_FOLK_WORKER_ENABLED=true
KONDO_FOLK_WORKER_INTERVAL_SECONDS=5
KONDO_FOLK_WORKER_BATCH_SIZE=1
KONDO_FOLK_PROCESSING_TIMEOUT_SECONDS=120
KONDO_FOLK_REQUEST_SPACING_SECONDS=0.25
```

`KONDO_FOLK_DRY_RUN=true` is the default. In dry-run mode the service returns the
folk writes it would make without calling folk.

Run locally:

```bash
python -m kondo_folk_sync.run
```

For Kondo to reach a local machine, expose port `8787` with a tunnel such as
ngrok or Cloudflare Tunnel and configure the Kondo webhook URL:

```text
https://<your-tunnel>/webhooks/kondo
```

If you set `KONDO_WEBHOOK_SECRET`, include it in Kondo's webhook auth field.
Kondo's Custom Webhook UI currently shows this as `API Key (optional)` with
the header name `x-api-key`, so paste the same secret value there.

The service also accepts this explicit header if your webhook tooling supports
custom headers:

```text
X-Kondo-Webhook-Secret: <your secret>
```

## Endpoints

- `GET /health` returns service status.
- `GET /events` lists recent sync attempts.
- `GET /console` shows the admin console.
- `GET /admin/stats` returns status counts, recent events, and retry candidates.
- `GET /admin/priority` returns the AI-ranked high-priority contacts.
- `GET /admin/triage` returns analyzed contacts in the console's daily triage
  format. Pass `since_hours=24` when you explicitly want a time window.
- `POST /admin/stage/{idempotency_key}` stages one reviewed row for the batch.
- `POST /admin/stage-all` stages all `review_pending` rows.
- `POST /admin/send-staged` queues all staged rows for folk writes.
- `POST /admin/request-full-sync/{idempotency_key}` marks one row as requiring
  Kondo manual full-history sync.
- `POST /admin/process` processes queued work slowly and safely.
- `POST /admin/reconcile` retries failed or interrupted events.
- `POST /admin/reprocess/{idempotency_key}` replays one stored payload.
- `POST /webhooks/kondo` receives Kondo webhook payloads.
- `POST /sync/manual` processes a payload manually for testing.

Admin endpoints require `KONDO_FOLK_ADMIN_TOKEN` in live mode. Pass it as either
`?token=...` for browser use or the `x-admin-token` header for scripts.

## Deployment

The service is deployable as a small web app. The included `Dockerfile` and
`render.yaml` are set up for an initial Render free-tier deploy:

- app command: `python -m kondo_folk_sync.run`
- public webhook: `https://<service-host>/webhooks/kondo`
- admin console: `https://<service-host>/console?token=<admin-token>`
- SQLite DB for the first deploy: `/tmp/kondo_folk_sync.db`

The first-deploy config intentionally does not attach a persistent disk, because
it is easier to create and validate. `/tmp` is ephemeral, so queue/history state
can be lost on restarts or redeploys. After the workflow is validated, upgrade
the Render service to a paid instance, add a disk mounted at `/data`, and change
`KONDO_FOLK_DB` to `/data/kondo_folk_sync.db`.

Recommended Render settings:

```bash
HOST=0.0.0.0
PORT=8787
KONDO_FOLK_RELOAD=false
KONDO_FOLK_DB=/tmp/kondo_folk_sync.db
KONDO_FOLK_DRY_RUN=false
KONDO_FOLK_AI_PROVIDER=auto
KONDO_FOLK_PROMPT_PATH=kondo_folk_sync/prompts/crm_analysis.md
KONDO_FOLK_RECONCILE_INTERVAL_MINUTES=60
KONDO_FOLK_REVIEW_MODE=true
KONDO_FOLK_WORKER_ENABLED=true
KONDO_FOLK_WORKER_INTERVAL_SECONDS=5
KONDO_FOLK_WORKER_BATCH_SIZE=1
KONDO_FOLK_PROCESSING_TIMEOUT_SECONDS=120
KONDO_FOLK_REQUEST_SPACING_SECONDS=0.25
```

Add these as Render secret env vars:

```bash
KONDO_WEBHOOK_SECRET=<shared Kondo webhook secret>
KONDO_FOLK_ADMIN_TOKEN=<private console token>
FOLK_API_KEY=<folk API key>
OPENAI_API_KEY=<OpenAI API key>
FOLK_GROUP_ID=<optional fallback folk group id>
FOLK_GROUP_CLAIMS_PROFESSIONALS_ID=<optional category group id>
FOLK_GROUP_DISTRIBUTION_PARTNERS_ID=<optional category group id>
FOLK_GROUP_TPAS_SUBROGATION_ATTORNEYS_ID=<optional category group id>
```

With `KONDO_FOLK_DRY_RUN=false` and `KONDO_FOLK_REVIEW_MODE=true`, the service
can write to folk, but only after a row is selected in the console and you click
`Send Selected Batch to folk`.

Configure Kondo's webhook in streaming mode for daily automatic capture. Keep
manual sync available for full-history refreshes from inside specific chats.

The worker and reconciliation loop do not scrape LinkedIn or poll Sales
Navigator. They only process Kondo events already received by the service. If
folk returns `429 Too Many Requests`, the event is moved to `retry_wait` using
folk's retry timing and processed later. If the process is interrupted while a
job is `processing`, the worker picks it back up after
`KONDO_FOLK_PROCESSING_TIMEOUT_SECONDS`.

## AI Layer

The editable AI prompt lives at:

```text
kondo_folk_sync/prompts/crm_analysis.md
```

Change this file to tune how the AI summarizes conversations, assigns reply
ownership, chooses follow-up dates, and decides what context belongs in folk.
Restart the service after editing it.

The AI layer emits structured JSON:

- `summary`
- `crm_note`
- `relationship_stage`
- `reply_owner`
- `next_action`
- `follow_up_date`
- `confidence`
- `meeting_detected`
- `important_context`
- `group_category`
- `group_reason`

If no AI key is configured, the service uses a conservative heuristic fallback so
local testing still works. Production should use `OPENAI_API_KEY` or
`ANTHROPIC_API_KEY`.

## folk Writes

When `KONDO_FOLK_DRY_RUN=false`, the service:

- upserts a folk person using LinkedIn URL as the local dedupe key and assigns
  the AI-selected group when that group's ID is configured
- creates a LinkedIn conversation interaction
- creates or updates one private note with AI summary and next action
- creates a private reminder when the AI decides you owe follow-up

Duplicate Kondo events are skipped using a hash of LinkedIn URL, latest message
timestamp, and latest message.

folk's public API can list groups and assign people to group IDs. Create these
groups in folk, then copy their IDs into `.env`:

- Claims professionals -> `FOLK_GROUP_CLAIMS_PROFESSIONALS_ID`
- Distribution partners -> `FOLK_GROUP_DISTRIBUTION_PARTNERS_ID`
- TPAs/subrogation attorneys -> `FOLK_GROUP_TPAS_SUBROGATION_ATTORNEYS_ID`

If a category-specific ID is blank, the service falls back to `FOLK_GROUP_ID`.
