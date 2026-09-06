# Operations and knowledge lifecycle

Load this reference only for orchestration, paid services, distributed Blender
execution, knowledge maintenance, or publication.

## Version state instead of overwriting it

**Applies when:** Inputs, prompts, skills, runtimes, evaluators, or prepared work
change after a usable result exists.

**Rule:** Treat the source baseline, prepared bundle, last complete render, and
terminal status as immutable versions. A changed contract creates a new version
with an explicit predecessor; a catalog refresh cannot silently replace
prepared work or erase a usable artifact.

**Verify:** The controller can identify which exact source and prepared bundle
were leased, which version superseded it, and which output remains the fallback.

**Search terms:** `immutable version`, `prepared bundle`, `supersedes`, `last known good`

## Make paid calls idempotent by exact request identity

**Applies when:** A model or paid API call may be retried across workers,
leases, restarts, or knowledge generations.

**Rule:** Key the logical call by task identity, stage, exact semantic input,
prompt version, model, endpoint, and wire request. Serialize lookup and creation.
Reuse a durable response; metadata-only drift cannot authorize another post.
A sent call with uncertain delivery remains non-replayable until reconciled.

**Verify:** At most one sender owns a logical call, the durable response is
replayed without network activity, and an uncertain send fails closed rather
than creating a duplicate billable request.

**Search terms:** `logical call`, `wire identity`, `sent unknown`, `idempotent API`

## Reserve budgets before any possible request

**Applies when:** Paid work has per-task, queue, token, or provider-credit caps.

**Rule:** Reserve the conservative maximum in a shared transaction before a
possible post. Reconciled debit plus active reservations stays within the local
authorization. Per-task and queue-wide ceilings are separate; a smoke ceiling
must not become a formal queue lifetime limit.

**Verify:** Reservations survive uncertain delivery, settle only against a
uniquely matched billing outcome, and release only calls proven unsent. Active
preparation cannot consume headroom reserved for required review stages.

**Search terms:** `credit reservation`, `queue budget`, `per-task cap`, `billing reconciliation`

## Classify provider responses before retrying

**Applies when:** A durable API response lacks usage, returns a client error,
or overlaps another reconciliation.

**Rule:** A usage-free response consumes no unreported debit. Non-retryable
request errors remain task-local and do not trigger the same request again.
Authentication, payment, permission, rate-limit, timeout, and uncertain-send
states retain global circuit behavior. A successful durable response awaiting
reconciliation must not poison unrelated calls.

**Verify:** Status and raw response are retained, reservation settlement follows
the response class, and retry decisions occur before expensive source/OCR/GPU
work.

**Search terms:** `provider error`, `usage missing`, `global circuit`, `non-retryable`

## Separate infrastructure recovery from semantic repair

**Applies when:** Remote timeout, GPU contention, engine attestation, process
termination, memory failure, dependency failure, or network outage interrupts a
task.

**Rule:** Infrastructure failures preserve semantic attempt count and prepared
work. Retry them only through bounded infrastructure policy. Missing source
dependencies remain real input blocks; GPU runtime failures are not relabeled
as asset defects, and neither class authorizes a model rewrite.

**Verify:** Terminal status names the failed layer and retry owner. No item stays
indefinitely `running`, no unrelated process is killed, and a completed remote
artifact is reused when only transport closure failed.

**Search terms:** `failure taxonomy`, `infrastructure retry`, `semantic attempt`, `dependency block`

## Keep CPU preparation outside GPU leases

**Applies when:** Work downloads, hashes, stages, OCRs, indexes, or prepares
assets before Blender rendering.

**Rule:** Complete bounded CPU/network preparation before acquiring the shared
GPU execution lock. GPU workers append terminal knowledge events but do not
embed, rebuild indexes, or switch knowledge generations. Retain any required
device holder only according to explicit deployment policy and release it when
the complete remote command is ready for the actual GPU lock.

**Verify:** Preparation and rendering have separate leases and concurrency
limits. Knowledge/index failure cannot retain a GPU, rerun Blender, or restart a
paid stage.

**Search terms:** `CPU preparation`, `GPU lease`, `outbox consumer`, `resource separation`

## Bound source acquisition by attempts, progress, and local bytes

**Applies when:** Videos or linked projects are downloaded or transferred.

**Rule:** Use bounded leased attempts, a wall-clock timeout, a no-byte-progress
timeout, resumable partial files, and a local cumulative byte cap. Remote
directory listings are not authoritative for nested size. After the configured
attempt ceiling, defer or reject visibly instead of monopolizing preparation.

**Verify:** Timeout releases only the downloader process group and its lease;
oversize is measured during local transfer; later recovery is explicit and
reuses task identity.

**Search terms:** `download timeout`, `no progress`, `cumulative bytes`, `bounded attempts`

## Verify GPU ownership with process and device identity

**Applies when:** Shared or newer GPU nodes require a reservation/holder before
render execution.

**Rule:** A lock name or session name alone is not ownership evidence. Require a
live descendant process, expected device identity, active compute presence, and
configured allocation. Prefer architecture-independent driver APIs for holder
code so a runtime built for older compute capabilities cannot falsely protect a
newer device.

**Verify:** Ownership evidence is fresh after reboot, tied to the claimed
device, and independent of Blender's later renderer attestation.

**Search terms:** `GPU holder`, `device identity`, `driver API`, `compute process`

## Prove that knowledge retrieval actually ran

**Applies when:** A prepared task claims to be knowledge-backed.

**Rule:** A populated version label is insufficient. Resolve one explicit
knowledge root across preparation and workers, validate its active manifest,
and retain a nonempty retrieval pack. A manifest-only generation uses local
lexical retrieval and does not require a vector index, embedding model,
network service, or an external worker. Never accept an implicit empty store.

**Verify:** The retrieval pack states active generation, query, selected rules,
and availability; a missing manifest or empty usable selection is a preparation
block. A deliberately manifest-only generation is available, not degraded.

**Search terms:** `knowledge root`, `active index`, `retrieval pack`, `empty fallback`

## Apply knowledge events asynchronously and fairly

**Applies when:** Accepted lessons are projected into a searchable knowledge
index, while terminal run events remain in control storage.

**Rule:** GPU work emits immutable diagnostic events to a zero-GPU consumer;
events are not themselves admitted knowledge. Only admitted success lessons
can update the active library. Rebuild and index switching are atomic
projections. A bounded consumer limits unapplied
events, not filenames scanned; already applied receipts are skipped before the
limit so early files cannot starve later knowledge.

**Verify:** Projection failure leaves source events intact, never reruns paid or
Blender work, and retries only the projection. Index switching exposes either
the prior complete generation or the new complete generation.

**Search terms:** `knowledge outbox`, `atomic index`, `projection retry`, `fair consumption`

## Promote rules only after independent validation

**Applies when:** A successful application suggests a reusable pipeline rule.

**Rule:** Keep a successful observation outside active retrieval as a candidate
until at least five distinct human-reviewed accepted assets cover the same
rule and an accepted unrelated holdout shows zero quality
regression. Route- and family-specific knowledge stays scoped; a repair from
one hair, cloth, material, or editing task cannot become a global default. A
repaired outcome without explicit acceptance is insufficient. Failed examples
remain diagnostics and cannot be promoted. A formally provable safety invariant
belongs in reviewed package guidance, with its proof obligation stated; it
cannot use the success-lesson promotion API to bypass empirical admission or
claim quality improvement.

**Verify:** Promotion records route, asset family, Blender version, one rule
identity, five unique accepted assets, named review IDs and artifact hashes,
and disjoint accepted holdout assets with human review and explicit
zero-regression results. The active row binds its source hash to the admission
decision. Automated findings are never mislabeled as direct human review.

**Search terms:** `knowledge promotion`, `candidate rule`, `independent holdout`, `anti-overfit`

## Store only useful guidance and admitted successful lessons

**Applies when:** A maintainer builds or updates the portable knowledge package.

**Rule:** `knowledge/manifest.json` explicitly lists the six maintained
Markdown references; the builder also includes the router and generation
entrypoints. These receive `curated` status, not fabricated human approval.
Run output directories are never crawled. Raw external JSON/JSONL manifests
may be read only through declared, contained paths, but their candidate rows
are excluded from the active index. Unreviewed render recipes and disabled
showcase catalog metadata are not successful knowledge. A success lesson enters
only through the promotion gate above, with `reviewed` status and a hash-bound
admission record.

**Verify:** Both index builders and both retrieval backends reject candidates,
deprecated entries, unapproved legacy rows, and unsuccessful outcomes. Mentioning
"failure" in a general safety rule does not make the rule a failed example.

**Search terms:** `active library`, `curated guidance`, `success-only admission`, `manifest allowlist`

## Keep candidates and diagnostics outside the active store

**Applies when:** A replay finishes or an older knowledge generation is updated.

**Rule:** A passing automated review can stage tutorial observations in the
sibling `<knowledge-root>_candidates/candidates.jsonl`, never in active retrieval.
`BLENDER_KNOWLEDGE_CANDIDATE_ROOT` may override that location only outside
`BLENDER_KNOWLEDGE_ROOT`. Failed or missing-review snapshots produce no chunks;
their existing review files remain diagnostic evidence. Updating an older
generation filters out non-admitted rows before atomically activating its
replacement. Previous immutable builds are not queried as a fallback.

**Verify:** A passing replay alone changes no active lesson, and a rejected
replay creates neither an active chunk nor a candidate lesson. No rebuild or
candidate write requires repeating a paid or Blender stage.

**Search terms:** `candidate staging`, `diagnostic separation`, `success observation`, `legacy cleanup`

## Chunk by decision boundary with explicit length limits

**Applies when:** Markdown guidance or a successful candidate tutorial is indexed.

**Rule:** Split at Markdown headings of levels one through four outside fenced
code and preserve nonempty text before the first heading. Repeated heading
titles receive deterministic occurrence suffixes. Pack whole blank-line-separated
paragraphs up to 2,200 characters for packaged guidance or 2,600 for tutorial
candidates. A single oversized paragraph is hard-sliced at that character
limit; no heading-free tail is discarded. Parts have deterministic title
suffixes and zero overlap. Packaged chunks shorter than 80 characters are
omitted. Source IDs hash source type, canonical path, and section/part title;
source hashes track normalized body content, so updates replace the same
logical section instead of duplicating it. This is character-based splitting,
not token, embedding, visual-frame, or sliding-window segmentation.

**Verify:** A long paragraph and a heading-free document retain all content,
each emitted body stays within its configured bound, and repeated ingestion
has a stable logical identity. Source text remains authoritative when a
retrieval excerpt ends before a complete rule.

**Search terms:** `chunk size`, `Markdown section`, `zero overlap`, `stable source identity`

## Retrieve locally before configuring optional vector search

**Applies when:** A fresh checkout or a portable launcher needs bundled guidance.

**Rule:** Set `BLENDER_KNOWLEDGE_ROOT` to the run's control directory and run
`build_blender_knowledge_index.py --manifest-only`. Then use the same root with
`retrieve_blender_knowledge.py --video-dir <dir> --output <pack.json>`. This
standard-library path requires no separate worker, private store, model
download, or Qdrant installation. Optional vector generations use cosine search;
an index error falls back only to the same active manifest. Lexical retrieval
scores query-word occurrence plus family and feature matches. Both paths filter
admission before exposing results; default top-k is ten and each pack excerpt
is capped at 1,400 characters. Source documents supply the complete rule.

**Verify:** The pack names its manifest, backend, query context, selected source
IDs, admission policy, and availability. Retrieved guidance never overrides
video evidence; only an exact executable canonical recipe with independent
approval can become a hard constraint. Curated guidance remains labeled as
guidance, not a proven-success recipe.

**Search terms:** `manifest lexical`, `offline retrieval`, `knowledge root`, `retrieval pack`

## Gate series expansion with artifact-bound review

**Applies when:** A category or tutorial series is about to fan out from a
canary.

**Rule:** Approve the exact canary artifact for subject, material, composition,
exposure, route, and presentation before expanding. File existence or automated
acceptance does not unlock a series. After approval, keep waves bounded and
review landed artifacts before further expansion; a category approval is not a
guarantee for every source scene.

**Verify:** The gate is bound to immutable artifact identity and invalidates
when the artifact changes or review is revoked. Unreviewed categories remain at
single-canary scope.

**Search terms:** `series canary`, `artifact-bound gate`, `bounded wave`, `fan-out`

## Preserve active leases during manifest replenishment

**Applies when:** A scheduler refreshes queued work while workers are preparing
or rendering.

**Rule:** Replenishment may supersede only queued nonterminal rows. It preserves
preparing and leased work with exact lease identity so live workers can commit
without duplication after restart. Schedule from retry-eligible claimable work,
not raw ready counts that include backoff.

**Verify:** A refresh does not change active lease IDs, create duplicate task
identity, or repeatedly fork no-op preparation for work still in backoff.

**Search terms:** `manifest replenishment`, `lease preservation`, `claimable ready`, `scheduler backoff`

## Publish route-specific results atomically

**Applies when:** A task becomes accepted, needs review, deferred, or blocked.

**Rule:** Publish a complete user-visible directory only for a real rendered
result, and keep preparation-only material in control storage. A result index
points directly to each visible output. Route metadata and filenames agree;
superseded attempts remain outside the public hierarchy. Derived publication
switches atomically after link and media validation.

**Verify:** Every visible result has an existing native scene and exactly the
required still or video, while pre-render terminals explicitly report no
artifact. Other public pages and outputs remain unchanged by a scoped publish.

**Search terms:** `atomic publish`, `result index`, `visible output`, `preparation terminal`

## Keep deployment configuration portable and private

**Applies when:** Code needs paths, hosts, ports, credentials, provider keys, or
daemon configuration.

**Rule:** Accept paths and endpoints through CLI, environment, or ignored
private config; ship portable defaults based on the package, user cache, or
temporary directory. Use key-based unattended transport and fixed known-hosts;
never commit passwords, cookies, tokens, private mount paths, or machine-specific
project identifiers.

**Verify:** The package sensitive-data checker passes, tests use temporary
directories, and a clean machine can import and inspect the CLIs without local
infrastructure.

**Search terms:** `portable config`, `environment variable`, `known hosts`, `secret scan`

## Recover disabled service state explicitly

**Applies when:** A supervised local daemon was intentionally disabled and is
later resumed.

**Rule:** Clear the platform's persistent disabled override before bootstrap;
an installation file alone does not prove the service can start. Report a
failed bootstrap as inactive rather than as a functioning worker or knowledge
consumer.

**Verify:** Service status shows a live expected process and fresh health state
after re-enable, without killing unrelated processes.

**Search terms:** `daemon resume`, `persistent disable`, `bootstrap`, `health state`
