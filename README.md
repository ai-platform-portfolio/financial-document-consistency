# Financial document consistency

Financial document AI pipeline for validating reusable platform infrastructure.

The first workload compares a financial report with its summary and returns
numerical or narrative discrepancies with paired source evidence. It exercises
document extraction, governed inference, durable background processing,
PostgreSQL and evaluated delivery through the portfolio's shared templates.

**Status:** review registration and persistence are implemented. Document upload,
extraction, processing, findings and user authentication are not implemented.
See [DESIGN.md](DESIGN.md) for architecture and later milestones. This API is a
local acceptance slice; it is not ready for public deployment.

## API contract

`POST /api/document-versions` registers immutable metadata, returning `201` with
the stored UUID and timestamp:

```json
{
  "content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_type": "report",
  "storage_ref": "packs/example/report-v1"
}
```

Use `report` or `summary` and a lowercase SHA-256 hash. `storage_ref` is a bounded
opaque identifier, not a URL; this API does not fetch or verify the object/content.
Registering identical metadata again creates a new document-version identity.

`POST /api/reviews` requires an `Idempotency-Key` header (1–128 letters, digits,
dots, underscores or hyphens, beginning with a letter/digit) and:

```json
{
  "report_version_id": "<registered-report-uuid>",
  "summary_version_id": "<registered-summary-uuid>",
  "check_profile_version": "financial-v1"
}
```

The versions must exist, have the indicated types and be distinct. The profile
is a frozen identifier; no checks are executed or profile catalogue validated yet.
The API returns `201` and `status: registered`. Identical concurrent retries return
the same stored review; different valid inputs with the same key return `409`.
Keys are scoped to this application's isolated database, not to authenticated
tenants; tenant-aware authorisation is a prerequisite for later public deployment.
`GET /api/reviews/{id}` returns the review or `404`; invalid requests return `422`.

Reviews and document versions are insert/read-only through this API/runtime role.
`registered` is derived from the persisted review's existence, not a queued or
completed processing state. Attempts and transitions arrive with the worker.
`/api/healthz` checks process health; `/api/readyz` checks required table columns.
Database/credential failures return generic `503` responses without driver errors.

## Local acceptance

Requires Python 3.12, Docker and an authenticated Atlas CLI. Create the Python
environment outside the repository to keep local dependencies out of Git:

```sh
python3.12 -m venv /tmp/financial-document-consistency-venv
/tmp/financial-document-consistency-venv/bin/pip install -r backend/requirements.txt
/tmp/financial-document-consistency-venv/bin/python tests/integration.py
```

The integration test owns a randomly named, pinned PostgreSQL 18.6 container with
an ephemeral loopback-only port. It generates credentials in memory, creates local
restricted migrator/runtime roles, applies the native Atlas migration and starts
the actual API. It accepts no external database URL. Tests cover concurrent
idempotency, conflicting inputs, constraints, role denials, migration reapply,
API restart persistence and database outage/recovery. The API/container/volume are
removed on completion. These local role fixtures are not deployment scaffolding.

Run native migration checks separately on disposable dev databases:

```sh
export DEV_DATABASE_URL='docker://postgres/18.6/dev?search_path=public'
atlas migrate validate --env local
atlas migrate lint --env local --latest 1
atlas migrate test --env local
```

`atlas.hcl` reads `DATABASE_URL` for an explicitly selected migration target;
never use the production database as a dev database. Application SQL contains no
roles, passwords or grants. Deployment uses the shared chart and operator; the API
does not run migrations at startup.

## Runtime

Build with `docker build -t financial-document-consistency-api backend`.
The runtime image is non-root and has no migration runner or test dependencies.
Run only on loopback for local development, supplying `DATABASE_URL` internally.
Use the restricted runtime connection, never the migrator/admin connection.

`DATABASE_AUTH=password` supports the local fixture. `DATABASE_AUTH=entra` uses
Azure's `DefaultAzureCredential` to request a short-lived PostgreSQL token for each
connection, with SDK caching/renewal. Configured workload identity is supported
without an Azure CLI dependency. Live Entra acceptance for this application remains
unverified. Deployment connection/TLS settings and identity belong to the chart;
the local fixture uses a disposable loopback connection without TLS.

Each request opens a connection with a three-second connect/statement timeout and
two-second lock timeout. Application pooling and capacity measurements are deferred.
No queue, model service, Azure resource or deployment is created by these commands.

## Hosted validation and publication

`.github/workflows/publish.yml` composes the existing shared Docker workflow and
Atlas actions for one API image and the migration directory. It runs on PRs,
`main` pushes and manual dispatches. It checks out the exact PR head or selected
commit, runs the existing HTTP/SQL acceptance tests, and validates migration
checksums/replay. Trusted publication also runs native migration lint/constraint
tests through `atlas.ci.hcl`, using disposable PostgreSQL only.

Same-repository PRs and `main` can publish. Fork PRs validate and build without
publishing credentials; paid Atlas lint/constraint steps are skipped for forks.
Manual dispatch from a non-main branch is build/validation only. Publishing uses
the existing organization `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID` and `ATLAS_CLOUD_TOKEN` secrets. Azure authentication is
OIDC, scoped to the image job; no runtime database credentials enter CI.
Missing credentials or incompatible Atlas repository permissions fail the run.

The pipeline ensures the application's `financial-document-consistency` Atlas
migration repository through the existing shared action, which verifies its
type/driver without modifying incompatible repositories. PR publication uses the
full source SHA tag with `latest: false`; only `main` publication advances the
main Registry baseline. Images use `portfolio/financial-document-consistency`
with distinct source-derived PR/main tags. Artifact publication is not deployment.

After both publishers succeed, the final job validates matching source identities,
image digest and local migration checksum. It rechecks the live PR/main head
immediately before uploading `artifacts.json`, retained for seven days. The record
contains source repository/SHA, workflow run/attempt/reference/event, API image
tag/digest/reference, and migration repository/tag/URL/`atlas.sum` SHA-256.
A failed or partial publisher produces no complete handoff record.

Freshness checking and uploading are not atomic. A later deployment consumer
must independently verify the successful authorized workflow attempt, current
source, image digest and migration content before selecting the artifacts.
Source-named tags alone are not registry-enforced immutability. No Helm release,
application registration, live database or public route is created here.
