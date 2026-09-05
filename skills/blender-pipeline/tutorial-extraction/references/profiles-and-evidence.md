# Sampling and bounded calls

All profiles use the supplied visual-tutorial skill and cover the complete
video in chronological groups. No interval is dropped to meet a call budget.

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

API and authenticated Codex CLI providers reuse the existing transport and
model identity checks. GPT-5.6-sol is the default; GPT-5.5 requires an explicit
fallback reason. There is no automatic model substitution.
