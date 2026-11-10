# Financial document consistency

Financial document AI pipeline for validating reusable platform infrastructure.

The first workload compares a financial report with its summary and returns
numerical or narrative discrepancies with paired source evidence. It exercises
document extraction, governed inference, durable background processing,
PostgreSQL and evaluated delivery through the portfolio's shared templates.

**Status:** source implements PDF upload, durable review processing, Foundry
extraction, evidence-backed checks and evaluated internal delivery. Local tests use
real PostgreSQL/workers and controlled model/storage responses; they do not prove
live Azure permissions or model quality. The shared data-only Blob role requires
provisioner authority before live acceptance. No user authentication or public
endpoint is provided. See [DESIGN.md](DESIGN.md) for boundaries and tradeoffs.

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
Legacy metadata-only versions remain unverified and produce `registered` reviews.
Verified uploads enter `queued -> running -> completed/failed`; review creation and
native Procrastinate job submission commit together. Only `financial-v1` is accepted
for processing. Completed responses include findings; unresolved checks are distinct
from processing failure. A maximum backlog of 100 queued/running reviews returns
`429` and rolls back unaccepted work. Same-key retries do not enqueue again.
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
API restart persistence, database outage/recovery, verified upload retries, atomic
queue rollback, worker interruption/recovery, duplicate delivery, backpressure,
retry exhaustion and malformed outputs. Test-only adapters replace Blob/Foundry;
production code contains no fake mode. The API/workers/container/volume are removed
on completion. These local role fixtures are not deployment scaffolding.

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
The runtime image is non-root and has no migration runner or test harness. Run the
same image with `python worker.py` for the worker. Its native readiness/liveness
command is `python worker.py --health`; it exposes no HTTP Service.
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
application registration, live database or public route was created by the initial
image/migration publication milestone.

## Complete application releases

`deploy/values.yaml` contains application intent: the API port/health paths and its
`reviews` database. CI supplies the source-matching API digest, migration reference
and expected revision through `scripts/write-release-values.py`. The mapper checks
the source, workflow attempt, image identity and migration checksum before writing
values. No chart templates or database administration live in this repository.

CI checks out the shared chart at the workflow's full `APPLICATION_CHART_REVISION`
pin and calls the existing `ops-shared` OCI publisher. The package contains the
unchanged shared chart, generated `release-values.yaml` and publisher-generated
`publication.json`. Its `appVersion` is the full application source SHA and its
version is `0.<workflow-run-number>.<attempt>-g<source-SHA>`.

Destinations are `releases/<owner>/<repository>/pr-<number>/application` for trusted
PRs and `releases/<owner>/<repository>/main/application` for main. One repository-
derived Atlas schema-plan repository is ensured for later operator role management;
it creates no database or SQL roles. Resource/database/runtime names remain derived
by the chart from the identity that deployment automation supplies.

The release tag is write-locked using the existing ACR action before the final
record is uploaded. `artifacts.json` adds `release.reference`, `release.digest` and
`release.chartRevision`. A failed package, push, lock or freshness gate withholds
the complete handoff. Deployment consumers still verify current source and the
successful authorized run; locking is not a substitute for those checks. Record
retention remains seven days; release/image/migration retention is separate.

No hostname or route is declared. A local render is an internal API Service plus
its dependencies, not a public endpoint or enforced network isolation. Environment
registration uses the preview intent below; persistent environments and promotion
remain separate steps.

## Internal PR previews

`deploy/previews.yaml` opts this repository into shared event-driven discovery.
The platform supplies repository identity; the shared chart derives each PR's
namespace, database, roles and client certificates. `generateHostname: false`
clears the automatic preview hostname. This API declares no explicit hosts/routes,
so no public ingress is rendered. This does not enforce network isolation or add
user authentication; readiness acceptance uses internal access.

Merge the shared discovery chart-pin adoption before this intent. Once the intent
is on `main`, open same-repository PRs can deploy their own successfully published,
source-matching releases through the native publication gate. The existing main
release is not reused as a PR release. No application registration is maintained
in GitOps and no manual database/credential setup is required.

The `reviews` database declares `reclaimPolicy: delete`: preview data is disposable.
Closing a PR triggers chart-owned ordered application/database/role/certificate
cleanup. Publication alone does not prove deployed API readiness or cleanup;
those require live acceptance after registration. Persistent data retention needs
separately reviewed intent before adding persistent environments.

With PyYAML 6.0.3 installed in a development environment, run the focused mapping
and real package/render tests against the workflow-pinned chart checkout:

```sh
APPLICATION_CHART=/path/to/pinned/application-helm-chart \
  python -B -m unittest discover -s tests -p 'test_publication.py' -v
```


## Upload-to-findings contract

`PUT /api/documents/{report|summary}/{upload_uuid}` accepts raw
`application/pdf`. Limits are 5 MiB, 20 text-bearing pages and 24,000 extracted
characters and 32,000 serialized input bytes per document. Upload identity is idempotent; different content/type
returns `409`. The server computes SHA-256, persists intent before the conditional
Blob write and marks a document verified only after that write. Retry completes a
partially committed upload without overwriting another object. Failed uploads are
never reviewable. Orphan intents/objects remain disposable until retry or preview
cleanup; this is not a cross-service transaction or a continuous garbage collector.

Only born-digital text PDFs are supported. Encrypted, scanned/image-only, empty
and oversized documents return `422`. Parsing runs in a child with a 12-second
wall deadline, eight-second CPU limit and Linux 512 MiB address-space limit.
API/worker memory limits include the parser; uploads are serialized per API process.
Page quotations are preserved; no OCR, table geometry or bounding-box claim is made.

Create the review with the existing `POST /api/reviews` contract and verified
version IDs. `GET /api/reviews/{id}` returns processing status/findings.
`GET /api/reviews/{id}/evidence` returns validated extraction steps, frozen config,
actual model identity and known token counts, separately identifying uncertain
attempts whose provider outcome did not commit. Content is returned only through
the internal API, not in application logs or traces.

The worker compares compatible entity/period/currency/scale/accounting-basis
claims with Decimal arithmetic (absolute tolerance 0.01 normalized currency units;
margin tolerance 0.1 percentage points). Missing/ambiguous comparators are
unresolved. One schema-constrained narrative call checks margin direction. Exact
quotes must occur on their cited page; schema validity does not establish truth.

## Durability and operational bounds

Procrastinate 3.10.0 uses the review transaction's external psycopg connection.
Its reviewed MIT schema is versioned in the Atlas migration, with its licence
notice; neither API nor worker installs schema at startup. Upgrade dependency
and vendor SQL together. API and worker SQL pools are bounded; listeners use direct
connections. No application SQL transaction or checked-out application connection spans a model
call; worker-control/listener connections remain. Queue traffic shares CNPG
with application data: this trades a broker/outbox boundary for database contention.

Notifications wake workers; a 30-second fallback and minute-level heartbeat-based
stalled-job recovery cover missed notifications and worker death. These are explicit
recovery mechanisms, not PR polling. Worker concurrency defaults to one. There are
three processing attempts, exponential provider backoff, 45-second model-call
limits, at most 12 reserved inference calls and 96,000 serialized input bytes per
review, and at most 3,000 output tokens per call. Admission bounds pending reviews.
No autoscaler or global cross-application model quota controller is implied.

A successful external model call can be repeated after a crash before result
commit. Validated completed steps are reused, terminal publication is idempotent,
and uncertain calls remain counted. Prompt/schema/check/release identities are
frozen; an incompatible worker release fails old queued work explicitly rather
than processing it under a different contract. Model deployment and returned
model version are bound on first use and checked on subsequent calls.

OpenTelemetry emits request/review/model/database spans to the existing collector.
JSON events record IDs, config, queue age, duration and outcome, without source
text. Token usage is persisted; missing responses are not reported as zero-cost.

## Evaluation and acceptance

```sh
python -B -m unittest discover -s tests -p 'test_processing.py' -v
python tests/integration.py
python tests/load.py > /tmp/document-pipeline-load.json
```

Versioned labelled numerical cases cover disagreements, unit scales, rounding,
period/currency/basis mismatches and ambiguous evidence. Real PDF extraction and
page/quote/value validation are tested separately. The document cases include labelled contradictions, consistency and missing
narrative evidence with an injected instruction, with separate revenue-extraction
and numeric/narrative labels. The integration fixture uses controlled model
responses to verify this harness; it is not a real-model semantic-quality score. `tests/load.py` owns its fixture and accepts no external
DB target. It compares 1k/10k/100k seeded reviews and one/two workers with synthetic
inference, reporting query plans, p50/p95, backlog, connections, CPU samples and
throughput. Local results are not live cluster capacity or provider throughput. The fault
fixture accelerates native heartbeat/recovery/fallback timers; production retains
the documented bounds.

For an explicitly selected, source-matching internal preview, use a loopback-only
Service tunnel and submit one bounded real-model acceptance pair:

```sh
python tests/deployed.py --base-url http://127.0.0.1:18000 --live-model --cases 3
```

A claim that fails evidence validation gets one corrected extraction request.
Both calls count toward the same review budget, and a second invalid result
fails the review. Logs record the validation reason without source or model text.

The deployed test writes disposable API records, checks upload retries, review
idempotency/conflict, persisted findings and evidence/model usage. Platform
acceptance separately verifies actual workload identities, cross-preview Blob
read/write denials, source/image provenance and close-triggered resource cleanup.
Live evidence belongs in the delivery record, not a claim inferred from CI.

### Live worker scaling

Use a source-matching disposable preview and a loopback-only tunnel to its API.
The first phase creates one synthetic PDF pair and submits four independent
reviews. Drain the queue, deploy the two-worker revision, verify both replicas,
and reuse the pair for the second phase.

```sh
python tests/load_live.py --base-url http://127.0.0.1:18000 --workers 1 --pair-file /tmp/document-load-pair.json --live-model
python tests/load_live.py --base-url http://127.0.0.1:18000 --workers 2 --pair-file /tmp/document-load-pair.json --live-model
```

Run each phase once. The worker argument labels the experiment. The operator
verifies replicas and provenance separately. Stop before the second phase if
the first fails, times out or shows throttling. The client emits review IDs,
observed completion times, API read timings, provider attempts and token usage.
Correlate those IDs with worker queue timing and read-only PostgreSQL activity
samples for the preview database. Treat the small burst as a comparison of the
two configurations and use the local fixture for larger row counts.
