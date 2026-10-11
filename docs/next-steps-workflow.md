# Next steps completion workflow

The task branch is based on `bce45d3` (2026. 10. 11.). The previous audit at
`3b93b74` was recovered from `/tmp/next-steps-audit` and checked against the newer
code before applying fixes. Local redeploy retirement, AWS/GCP node pools, and
the analyzer → deployer → healer connection already existed in the newer base.

## Run validation

```bash
uv sync --project backend --frozen
npm ci --prefix frontend
cd frontend && npx playwright install chromium && cd ..
scripts/next-steps-workflow.sh
# Include the 11 real Docker scenarios in copied temporary directories:
scripts/next-steps-workflow.sh --docker
# Also exercise the optional Langfuse SDK with an in-memory exporter:
scripts/next-steps-workflow.sh --docker --tracing
```

The workflow runs backend lint, format checks and pytest, then frontend unit
tests, lint, build and browser tests. It records each stage in its own log under
a newly created `/tmp/cloudmorph-next-steps.*` directory and returns nonzero if
any stage fails. `--no-browser` explicitly skips browser checks. Docker checks
disable tunnels, tracing and memory; the verifier forbids model fallback and
cleans up its own deployment handles. Existing containers are not swept.

## Agent Memory

Memory is disabled by default so repeated demo rehearsals still show the healing
loop. Enable it on the backend with:

```dotenv
CLOUDMORPH_HEAL_MEMORY=1
# Optional; default backend/.cloudmorph/heal-memory.sqlite3
# CLOUDMORPH_HEAL_MEMORY_FILE=/private/path/heal-memory.sqlite3
# CLOUDMORPH_HEAL_MEMORY_DAYS=30
```

After a deployment fails, safety stops and deterministic rules still run first.
Memory can supply a previously verified Dockerfile fix before model fallback.
It does not skip the initial deploy or its verification. Scope includes owner,
app, source identity, target, source contents, original Dockerfile and error
fingerprints. Changed source, Dockerfile or target causes a miss. Source edits
are excluded; a repository-provided Dockerfile still goes through the existing
confirmation flow. Failed replay is invalidated.

The private SQLite store retains hashes and narrowly allowlisted changed lines,
not full source, stderr, Dockerfiles, environment values or plaintext identities.
It keeps up to 20 entries per scope and 200 globally, with 30-day inactivity
expiry. Unsupported edits are not learned. Storage failures leave normal healing
available. Memory hits retain the ordinary diff report and emit a `heal_memory`
log event.

## Langfuse tracing

Install the optional SDK and explicitly enable tracing:

```bash
uv sync --project backend --extra tracing
```

```dotenv
LANGFUSE_TRACING_ENABLED=true
LANGFUSE_PUBLIC_KEY=<project public key>
LANGFUSE_SECRET_KEY=<project secret key>
# LANGFUSE_BASE_URL=https://cloud.langfuse.com
```

Each deployment has an opaque trace ID with analyze, deploy and heal spans;
analyzer worker threads keep their parent context. Model observations include
provider, model and token usage when supplied by the provider. Retries remain
under the deployment trace. Prompts, responses, source, repository identifiers,
paths, stderr and environment values are excluded. Metadata accepts only fixed
operational names, enums, booleans and bounded counts. Tracing is opt-in and
fail-open; shutdown waits at most two seconds.

## Measurement boundary

Local Docker scenarios cover all 11 broken apps, including dependency, port,
permission and requirements repairs and intended service/configuration/crash/app
stops. Cloud Run blue/green failure and rollback behavior is covered by offline
tests. Synthetic Cloud Run startup tests are separate from captured fixtures.

Live Cloud Run stderr measurements require an authenticated `gcloud` executable
and an agreed GCP project; `gcloud` is absent from this WSL environment. The
existing `broken-import.cloudrun.stderr.txt` is the only captured Cloud Run
fixture. Expanded live measurements and a real Langfuse project ingestion check
remain external validation steps, and are not reported as completed.
