# Sampling and bounded calls

## Visual mode

All visual-mode profiles use the supplied skill and cover the complete
video in chronological groups. No interval is dropped to meet a call budget.
Videos at most 10 minutes long are recommended for this method; longer inputs
are supported and do not automatically switch to another method.

| Profile | Under 2 min | 2–10 min | Over 10 min | Focused requests/group | Calls/10 min |
| --- | --- | --- | --- | --- | --- |
| economy | 4 s | 10 s | 30 s | 8 | 8 |
| balanced | 2 s | 5 s | 15 s | 18 | 16 |
| forensic | 1 s | 2.5 s | 7.5 s | 30 | 32 |

Each group normally covers up to three minutes. One pass examines contact
sheets and requests decisive timestamps; a second inspects those individual
frames and produces a corrected ledger. Complete prose is composed globally,
then the rubric is generated separately. At most one format/rubric repair is
allowed. `--max-calls` sets an explicit total cap of at least five.

The historical `--window-budget` argument now limits the number of groups,
not coverage: smaller budgets enlarge groups. Zero means automatic. The
reproduction launcher's positive-budget requirement remains unchanged.

Exact parameters and wiring must point to an inspected source frame. OCR and
transcripts assist localization; they do not replace visual confirmation.
Unclear values remain unspecified or clearly labeled recommendations while
preserving the operation and its context. Reuse cached analysis on retries.
A source frame supplies the opening final-result image; do not render a substitute.

## Legacy-rich mode

The original production recipe keeps 5-second frame sampling and complete
60-second windows under every profile. Each window uses the maintained rich
prompt and its source contact sheet, ASR and optional OCR. Results are merged
in order with the production merger; no global visual-mode rewriting or rubric
call replaces this path. The production default evidence-image transport size
is 640 pixels on the long side; `RICH_TUTORIAL_IMAGE_MAX_SIDE` may explicitly
override it (320–4096).

There is one primary call per chronological window. The default total cap is
`ceil(duration_seconds / 60) + 1`, reserving at most one format-only repair
across the run. An explicit lower `--max-calls` fails before model calls. A
positive `--window-budget` must cover every required 60-second window; zero or
omission is automatic. This adapter intentionally does not use the production
preparer's historical evenly sampled window limit, because that would drop
parts of the requested end-to-end procedure.

Only a parsed but malformed steps object can receive the shared format repair.
Malformed transport JSON, uncertain delivery, provider failure and exhausted
budgets stop safely without automatic weaker prompts or model substitution.
Resolved transcripts, prepared evidence and normalized responses are cached
outside the learner workspace and reused on reruns.

## Providers and models

API and authenticated Codex CLI providers reuse the existing transport and
model identity checks. GPT-5.6-sol remains the default. API mode accepts the
provider model ID supplied by the user, including deployment aliases, without
requiring a fallback reason. Codex mode retains GPT-5.6-sol and explicit-reason
GPT-5.5 fallback. There is no automatic model substitution.
