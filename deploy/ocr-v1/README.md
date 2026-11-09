# OCR v1 deployment records

`deploy/ocr-v1/sandbox.toml` is the authoritative machine-readable record for the current development deployment. Read it before any Cloud Run, Artifact Registry, Scheduler, IAM, GCS, n8n, or deployment-verification work.

The manifest records observed external state, not desired-state automation. Its `source.commit` identifies the code used to build the deployed image; later documentation or CI-only commits do not change that image. Update the manifest only after reading the exact resources back from GCP, and keep the matching redacted evidence report under `verification/deployments/`.

Do not copy mutable project, identity, image, revision, URL, or schedule values into `AGENTS.md`. Keep those facts here so a deployment change is a one-place edit.

A deployment is not OCR acceptance. The current manifest separately records health, paid request count, n8n acceptance, and real-engine accuracy status.
