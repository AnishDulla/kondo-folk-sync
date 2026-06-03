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

This repo includes `render.yaml` and `Dockerfile`.

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

After deployment:

```text
https://<render-service>.onrender.com/health
https://<render-service>.onrender.com/console?token=<admin-token>
https://<render-service>.onrender.com/webhooks/kondo
```

More detail lives in `docs/kondo_folk_sync.md`.
