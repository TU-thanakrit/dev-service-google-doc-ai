# Verification summary

- Timestamp: `2026-10-25T20:11:19+07:00`
- Workspace: `/home/danny/service_google_doc_ai`
- gcloud account: `thanakrit.boonquarmdee@thaiunion.com`
- Project: `thaiunion-ai-platform` (`608705486562`)
- Region: `asia-southeast1`
- Document AI processor: `tu-ddai-ocr` (`62bd4850f12a7d55`)
- Processor state: `ENABLED`
- Processor version: `pretrained-ocr-v2.1.1-2025-01-31`

## Local implementation verification

- Unit/API/integration tests: `30 passed`
- Application coverage: `95%`
- Ruff: no issues
- Python import/compile checks: passed
- Podman image: `localhost/service-google-doc-ai:local` built successfully

## Live Google Cloud API verification

Every test PDF came from a public internet source recorded in `examples/manifest.json`. The local FastAPI endpoint called the live Document AI processor and Vertex AI Gemini model.

- `cash_bill.pdf`: HTTP 200; expected/detected `บิลเงินสด`; 11 exact extraction keys
- `receipt_substitute.pdf`: HTTP 200; expected/detected `ใบรับรองใบแทนใบเสร็จ`; 10 exact extraction keys
- `tollway.pdf`: HTTP 200; expected/detected `ทางด่วน`; 3 exact extraction keys
- `tax_invoice.pdf`: HTTP 200; expected/detected `ใบกำกับภาษี`; 12 exact extraction keys
- `receipt.pdf`: HTTP 200; expected/detected `ใบเสร็จรับเงิน`; 9 exact extraction keys

Overall result: `5/5 passed`.

Local-only full API response evidence was written to
`verification/local-live-e2e-report.json` during the approved test run. It is
excluded from Git because OCR and extracted values may contain sensitive data.

Report SHA-256: `0eab0604381c9b76fbcdae75feb3a2152b455f438a2b135bb5f7e3712b6d0b83`

Input hashes: `verification/example-pdfs.sha256`

## Cloud Run deployment blocker

Cloud Run deployment could not be completed under the requested user account because the account received these authorization failures:

- `serviceusage.services.enable` denied while enabling `cloudbuild.googleapis.com`
- `iam.serviceAccounts.create` denied while creating the runtime service account
- `artifactregistry.repositories.uploadArtifacts` denied while pushing the locally built container

The exact administrator prerequisite commands are documented in `README.md`. No existing Cloud Run service was modified.
