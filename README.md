# Kondo folk Sync

Daily review service for syncing Kondo LinkedIn/Sales Navigator conversations into folk.

The workflow is:

1. Kondo sends conversation webhooks to this service.
2. The service analyzes the contact and conversation.
3. The console holds contacts for review.
4. You select latest-message or full-history recaps.
5. folk is updated only when you click `Send Selected Batch to folk`.

## Local Run

```bash
python -m pip install -r requirements.txt
python -m kondo_folk_sync.run
```

Open:

```text
http://127.0.0.1:8787/console?token=<KONDO_FOLK_ADMIN_TOKEN>
```

## Render Deploy

This repo includes `render.yaml` and `Dockerfile`. The checked-in Blueprint is
configured for a low-friction first deploy on Render's free web service. It uses
ephemeral SQLite storage at `/tmp/kondo_folk_sync.db`, so queue/history state can
be lost on restarts or redeploys. After the workflow is validated, upgrade the
service and add a persistent disk.

Deploy from Render as a Blueprint, then set these secret environment variables:

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

For a shared team deployment, keep one `FOLK_API_KEY` for the shared folk
workspace and replace the single webhook/admin secrets with per-user config:

```bash
KONDO_FOLK_USERS_JSON='[
  {
    "slug": "anish",
    "name": "Anish",
    "admin_token": "<anish console token>",
    "webhook_secret": "<anish kondo webhook secret>"
  },
  {
    "slug": "teammate",
    "name": "Teammate",
    "admin_token": "<teammate console token>",
    "webhook_secret": "<teammate kondo webhook secret>"
  }
]'
```

Team URLs:

```text
https://<render-service>.onrender.com/console/anish?token=<anish-console-token>
https://<render-service>.onrender.com/webhooks/kondo/anish

https://<render-service>.onrender.com/console/teammate?token=<teammate-console-token>
https://<render-service>.onrender.com/webhooks/kondo/teammate
```

Each user sees only their own Kondo review queue. Both users write to the same
folk workspace. The service adds LinkedIn owner attribution to synced CRM notes
for team users.

After deployment:

```text
https://<render-service>.onrender.com/health
https://<render-service>.onrender.com/console?token=<admin-token>
https://<render-service>.onrender.com/webhooks/kondo
```

More detail lives in `docs/kondo_folk_sync.md`.

## Hard Reset

To wipe folk and rebuild from Kondo latest-message syncs:

```bash
python scripts/reset_folk_people.py
python scripts/reset_folk_people.py --execute --confirm DELETE_ALL_FOLK_PEOPLE
```

The first command exports a backup and does not delete anything. The second
command exports a fresh backup, then deletes every folk person.

After deleting folk, open the console Advanced tools and type `RESET` under
`Reset Local Sync State`. Then bulk-sync the desired Kondo conversations into
the webhook and review them before sending to folk.
