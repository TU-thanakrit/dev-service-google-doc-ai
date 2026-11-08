# OCR v1 — n8n upload → poll → result (issue #12, local artifact only)

Status: **a guide and an inactive definition, nothing verified.** No n8n instance
has been contacted, no workflow has been imported, activated, or executed, and the
identity-token path below is a design that must be proven against the deployed
service and the real n8n runtime (`docs/ocr-v1-contracts.md`, B2). Do not activate
anything from this document without explicit approval, a deployed service URL, and
a document/spend budget.

Artifact: `examples/n8n/ocr-upload-poll-result.workflow.json` — `active: false`, no
credentials, no tokens, no keys. It is a definition to import by hand, not
something this repository runs. `tests/test_n8n_workflow_artifact.py` guards its
shape and that it embeds no secret.

## What the workflow does

| Node | Type | Role |
|---|---|---|
| Manual Trigger | `manualTrigger` | Operator-started only; the workflow has no schedule and no webhook trigger |
| Config | `Set` | `serviceUrl`, `documentPath`, and an `idempotencyKey` derived from the execution id |
| Get identity token | `HTTP Request` | `GET http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience=<serviceUrl>`, header `Metadata-Flavor: Google` |
| Read document | `Read/Write File` | Reads `documentPath` into the item's binary data |
| Submit document | `HTTP Request` | `POST {{serviceUrl}}/v1/ocr/jobs`, multipart `file`, headers `Authorization: Bearer <token>` and `Idempotency-Key` |
| Wait | `Wait` | 10 s between polls (see the shortcut below) |
| Get status | `HTTP Request` | `GET {{serviceUrl}}/v1/ocr/jobs/{job_id}` |
| Job final? | `IF` | True when `state` is `succeeded`, `partially_succeeded`, or `failed` |
| Get result | `HTTP Request` | `GET {{serviceUrl}}/v1/ocr/jobs/{job_id}/result` |
| Result summary | `Set` | Keeps `job_id`, `state`, `reason`, `completeness`, `page_count`, `warnings` — not the text |

The loop is `Submit → Wait → Get status → Job final?`; the false branch returns to
`Wait`, the true branch retrieves the result. `job_id` comes from the submit
response, so a `202` that only acknowledges a launch is enough to start polling.

## The identity token

The service is private and re-verifies the caller's ID token in-process, so the
token's audience must be exactly the service URL (`OCR_API_AUDIENCE`) and its
identity must be mapped in `OCR_TEAMS`. The artifact uses the **GCP metadata
server** path, which needs no credential in n8n and works only when n8n itself runs
on GCP with the intended service account attached to its instance/VM.

Off GCP, the equivalent paths are `iamcredentials.generateIdToken` (which needs an
n8n credential you must create locally — never export it into this artifact) or
workload identity federation. Which path the real n8n deployment uses is
**unverified** [B2]; the configuring human's identity is not assumed to be the
runtime caller.

Prerequisites for the caller identity, none of which has been granted in this
repository's name:

- `roles/run.invoker` on the deployed service;
- the same principal present in `OCR_TEAMS` mapping to its internal `team_id`;
- for a person running this by hand off GCP, `roles/iam.serviceAccountTokenCreator`
  on that service account (what `scripts/verify_ocr_service.py --impersonate`
  needs).

## Importing it safely (operator steps)

1. Wait for the deploy: the service URL, the runtime service account, and the
   `run.invoker` grant must exist. None of that is done yet — see
   `docs/ocr-v1-cutover.md` and the administrator prerequisites in `README.md`.
2. In n8n: **Workflows → Import from File**, choose the artifact.
3. Open the `Config` node and set `serviceUrl` to the deployed service URL and
   `documentPath` to a path the n8n process can read.
4. Re-select node type versions if n8n offers an upgrade; the artifact pins the
   versions it was written against and its import has **not** been tested.
5. Leave the workflow inactive and use "Execute workflow" for a single manual run
   on an approved, non-sensitive document, so the run can be stopped and its
   execution data deleted.
6. Record the observed status codes, `job_id`, state, page accounting, and the
   service's own logs as evidence. Keep the recognized text out of any file that
   leaves the machine.

## Shortcuts and limits (deliberate, not oversights)

- **Fixed 10 s wait, no backoff.** The artifact does not read `Retry-After`. A full
  admission set answers `429 capacity_exceeded` with `Retry-After: 30`, and a
  not-final result answers `202` with `Retry-After: 10`; a production workflow
  should honor both. Upgrade when real polling behavior is observed.
- **One file per execution**, PDF only in the multipart part. Submission of JPEG,
  PNG, and TIFF is supported by the service and needs the part's media type
  changed; the artifact submits `application/pdf`.
- **Execution data is sensitive.** n8n stores node input/output payloads, and
  `Get result` receives the recognized text. The service's own logs are content-free
  (contract D20), but n8n's execution store is not: treat it as document data,
  prune it, and never export it as evidence.
- **No retry of a failed job.** Resubmitting the same `Idempotency-Key` with the
  same bytes returns the existing job while its record is live; the artifact
  derives the key from the execution id, so a new execution is a new job by design.
- **Nothing here is acceptance.** Fake-engine tests and an imported workflow are
  local evidence only; the ticket's cloud and n8n acceptance items stay open until
  they are run against the deployed service.
