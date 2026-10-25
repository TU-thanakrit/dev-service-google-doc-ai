# service_google_doc_ai

POC API for five Thai general-expense document templates. It uses Google Document AI Enterprise OCR, Vertex AI Gemini structured output, and Cloud Run.

## Supported templates

- บิลเงินสด
- ใบรับรองใบแทนใบเสร็จ
- ทางด่วน
- ใบกำกับภาษี
- ใบเสร็จรับเงิน

`สิ่งที่อ่าน` always contains exactly the configured keys for the detected template. Missing source values are `null`. LLM-derived `Type of Expense` is returned separately under `Analysis`.

## Local development

```bash
uv sync --python 3.12
uv run pytest
uv run ruff check .
```

Copy `.env.example` to `.env` and provide Google Application Default Credentials before calling the real cloud services.

```bash
uv run uvicorn app.main:app --reload
```

For a temporary local live test with the active `gcloud` account, without saving an access token:

```bash
export GOOGLE_CLOUD_PROJECT=thaiunion-ai-platform
export DOCAI_LOCATION=asia-southeast1
export DOCAI_PROCESSOR_ID=62bd4850f12a7d55
export VERTEX_LOCATION=asia-southeast1
export GEMINI_MODEL=gemini-2.5-flash
uv run python -m scripts.serve_with_gcloud
```

## API

```bash
curl -X POST \
  -F 'file=@example.pdf;type=application/pdf' \
  http://localhost:8000/v1/documents:process
```

## GCP target

- Account: `thanakrit.boonquarmdee@thaiunion.com`
- Project: `thaiunion-ai-platform`
- Cloud Run region: `asia-southeast1`
- Document AI location: `asia-southeast1`
- OCR processor: `62bd4850f12a7d55`

## Deploy

An administrator must first enable Cloud Build, create the runtime service account, grant its runtime roles, and allow the deployer to attach it:

```bash
gcloud services enable cloudbuild.googleapis.com
gcloud iam service-accounts create service-google-doc-ai \
  --display-name='Document AI POC Cloud Run'
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member='serviceAccount:service-google-doc-ai@thaiunion-ai-platform.iam.gserviceaccount.com' \
  --role='roles/documentai.apiUser'
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member='serviceAccount:service-google-doc-ai@thaiunion-ai-platform.iam.gserviceaccount.com' \
  --role='roles/aiplatform.user'
gcloud iam service-accounts add-iam-policy-binding \
  service-google-doc-ai@thaiunion-ai-platform.iam.gserviceaccount.com \
  --member='user:thanakrit.boonquarmdee@thaiunion.com' \
  --role='roles/iam.serviceAccountUser'
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member='user:thanakrit.boonquarmdee@thaiunion.com' \
  --role='roles/run.sourceDeveloper'
gcloud projects add-iam-policy-binding thaiunion-ai-platform \
  --member='user:thanakrit.boonquarmdee@thaiunion.com' \
  --role='roles/serviceusage.serviceUsageConsumer'
```

Then deploy:

```bash
gcloud config set account thanakrit.boonquarmdee@thaiunion.com
gcloud config set project thaiunion-ai-platform
gcloud config set run/region asia-southeast1

gcloud run deploy service-google-doc-ai \
  --source . \
  --region asia-southeast1 \
  --service-account service-google-doc-ai@thaiunion-ai-platform.iam.gserviceaccount.com \
  --no-allow-unauthenticated \
  --memory 1Gi \
  --timeout 300 \
  --concurrency 4 \
  --set-env-vars GOOGLE_CLOUD_PROJECT=thaiunion-ai-platform,DOCAI_LOCATION=asia-southeast1,DOCAI_PROCESSOR_ID=62bd4850f12a7d55,VERTEX_LOCATION=asia-southeast1,GEMINI_MODEL=gemini-2.5-flash
```

## End-to-end verification

`examples/manifest.json` records each internet source and expected template. The verifier fails unless every request returns HTTP 200, the expected template, and exactly the corresponding extraction and analysis keys.

```bash
SERVICE_URL=$(gcloud run services describe service-google-doc-ai \
  --region asia-southeast1 --format='value(status.url)')

uv run python -m scripts.verify_examples \
  --base-url "$SERVICE_URL" \
  --gcloud-auth
```

The verifier stores only response metadata and output-key names by default. Use
`--include-responses` only for an explicitly approved local evidence run because
full OCR and extracted values may contain sensitive document data.
