# Reproduction execution contract

The default knowledge root is `reproduction/knowledge` and contains exactly one
`manifest.json`. Its `recipes` field is keyed by recipe ID. A runnable recipe
must be explicitly enabled, use a catalog-declared executable coverage status,
have no gaps, resolve all evidence inside the skill package, and pass both the
distribution and visual-equivalence gates. Missing, false, unknown, malformed,
or contradictory values fail closed.

The current public catalog is a metadata-only authority snapshot. It tracks 61
public items at website commit
`1115f547a5ecccdeacd0e2dde396326f040b89f6`; every imported recipe remains
disabled because none has an independent visual-equivalence receipt. Public
media URLs and their SHA-256 attestations are acceptance references only. They
must never be treated as source-asset download locations.

## Hybrid distribution contract

Every recipe declares `schema_version: "1.0"`, `policy_mode: "hybrid"`,
`code_distribution: "open-source"`, `auto_download_allowed: false`, and:

- `asset_delivery`: `bundled-open`, `user-supplied`, or `blocked`.
- `requires_user_asset`: consistent with the delivery mode.
- `source_license_status`: `verified-open`, `restricted`, `unknown`,
  `not-applicable`, or `blocked`.
- `open_source_contents`: exactly `recipe`, `adapter`, `parameters`, and
  `verification-contract`; source assets and private tutorial media are not in
  this scope.
- `asset_provenance`: evidence status, nullable license identifier, and local
  evidence references.
- `user_asset_verification`: accepted SHA-256 values and/or mandatory semantic
  validation for a user-supplied asset.
- `bundled_asset_relative_path`: package-relative only for `bundled-open` and
  null otherwise.

`bundled-open` additionally requires the exact source file inside the knowledge
package, `verified-open` license status, package-local license evidence, a
non-empty license identifier, and an accepted SHA-256. `user-supplied` requires
an explicit local path and never permits automatic download or redistribution.
`blocked` never plans or executes.

The catalog-level `distribution_policy.code_license` controls code publication.
`pending-selection` requires null license fields and
`publication_ready: false`. Do not infer a license from repository visibility
or choose legal terms on the owner's behalf.

## Tutorial preparation

`--tutorial-mode provided` stages only the supplied authorized tutorial files.
`--tutorial-mode extract` is the explicit paid preparation route:

1. validate the recipe and its distribution/visual gates;
2. validate an HTTPS provider endpoint and owner-only secret file;
3. stage the reference image and download the requested video with `yt-dlp`;
4. sample timestamped evidence, ASR/OCR, and bounded 60-second windows;
5. call the selected model per complete window, merge the results, and emit
   `tutorial.md`, `steps_verified.json`, and the visual contract;
6. optionally render `illustrated_tutorial.html` from those same steps when
   `--render-tutorial-html` is present;
7. retrieve maintained knowledge, generate Blender code, execute the selected
   route, and validate editable scene plus fresh visual/motion evidence.

Dry run ends after contract validation and plan emission. It performs no
download, credential read, model call, Blender launch, or workspace write.

## Entrypoints and inputs

Supported entrypoints are an argv-only Python command or a Blender project plus
package-local reproduce script. Shell execution is not supported. Accepted
placeholders are `{run_manifest}`, `{video_dir}`, `{run_dir}`, `{output_dir}`,
`{asset}`, `{image}`, and `{tutorial}`.

Accepted sources are one HTTP(S) video URL, repeated tutorial inputs and
repeated reference images supplied either as local files or explicit HTTP(S)
URLs, and one local source asset/project. Remote source assets are rejected.
Staged user assets remain run-local and must not enter version control.
