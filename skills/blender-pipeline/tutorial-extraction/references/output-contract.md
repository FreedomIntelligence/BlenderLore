# Selectable tutorial and pipeline contracts

The canonical `extract_video_tutorial.py` exposes `--tutorial-method visual|legacy-rich`.
Omitting it selects `visual`, the supplied `video-to-visual-tutorial` skill.
Both methods accept the same local video/URL, provider, model, transcript, input
assets, output directory and workspace switches. Choose the tutorial method
independently from `--provider api|codex-cli`; neither choice changes the other.

## Visual (default)

Recommended source duration is at most 10 minutes; this is a recommendation,
not a hard restriction. The supplied skill's learner package is retained:

```text
<subject>/
  input/
  output/
    <subject>教程.md
    <subject>验收评分Rubric.json
    image/
```

Run with `--video-file` or `--video-url`, `--title`, and `--output-dir`.
Install this skill's `requirements.txt` in the pipeline Python environment;
the generation environment also includes the optional Markdown HTML renderer.
Use `--input-asset` for each actual learner input, including a starter blend or
a texture/dependency folder. Asset names are preserved. Missing required assets
stop publication. No source video, OCR or analysis cache belongs in `input/`.
The default target versions remain 4.1 and 5.1.2; the document must disclose
that cross-version execution has not been checked when no such test occurred.

Standalone extraction puts the subject folder below `--output-dir`.
`--workspace-mode` puts it below `--output-dir/tutorial_package/` and writes
these derived replay files at the workspace root:

- `tutorial.md` and `tutorial_path_refs.md`: the same full procedure, with rebased local images and input paths.
- `steps_verified.json`: an ordered projection of the final tutorial, with source claims. Its schema identifies the visual skill; it does not claim the old four-frame Claim Q-Gate ran.
- `tutorial_visual_contract.json` and `rich_evidence/windows.json`: existing generation consumers' views of the same procedure and source frames.
- `tutorial_manifest.json`: package paths, source identity, model/cost summary and adapter version.

The manifest schema is `video2blender-visual-tutorial.v1` and records
`tutorial_method: visual`. The JSON rubric stays inside the subject package and is not read into the
Blender task prompt. Tutorial extraction only documents the workflow; it does
not execute the embedded verifier or build/render a solution.

## Legacy-rich (original production recipe)

Select `--tutorial-method legacy-rich` to run the production evidence preparer,
original rich-window prompt and normalization, production chunk merger and
base64 image embedder. The API/Codex transport is shared with visual mode;
the tutorial content is not produced by visual mode or the failed v2 fragment
extractor. The production generator's maintained `build_prompt` is imported,
not copied into a second drifting prompt implementation.

The standalone subject package is:

```text
<subject>/
  input/
  output/
    tutorial.md
    tutorial_path_refs.md
    image/
```

`tutorial.md` preserves the rich chronological procedure with base64 images.
`tutorial_path_refs.md` contains the same procedure with ordinary relative
image paths. The normal files in `image/` remain independently usable. This
mode does not produce a visual-skill rubric. Actual `--input-asset` files and
folders are copied to `input/`, preserving dependency directories; no source
video or intermediate extraction evidence belongs in this learner package.

In workspace mode, `tutorial.md` and `tutorial_path_refs.md` are rebased views
of the subject package. The original `steps_rich.json` and visual text contract
are retained. `steps_verified.json` is explicitly labeled a production-rich
projection, not an independent Claim Q-Gate result. `rich_evidence/windows.json`
retains all chronological evidence intervals. Its manifest schema is
`video2blender-legacy-rich-tutorial.v1`, with `tutorial_method: legacy-rich`,
source URL and SHA-256, complete coverage, recipe identity and genuine provider
usage. The workspace validator checks this method's own contract, without
requiring the visual rubric or claiming the historical v2 Q-Gate ran.

## Shared workspace and storage behavior

`--render-html` adds `illustrated_tutorial.html` at the workspace root,
outside the subject package's `output/`, using the same Markdown without a
model call. The method's learner-output contract is preserved. Generation's
`--render-tutorial-html` forwards to this behavior.

Analysis lives beside the workspace under `.video-tutorial-cache/` by default;
`--cache-dir` overrides it. The cache must be outside the tutorial workspace.
It stores timestamped frames, resolved ledgers and normalized model results to
resume retries. URL downloads are temporary. Provider secrets and raw transport
responses are not included in the learner package.

Legacy v2 package validation remains available only for historical results;
`legacy-rich` is not that algorithm. A new standalone run needs an unused subject
output path. Explicit generation `--force-tutorial` forwards `--replace-existing`,
which replaces only the previous manifest-owned subject package after staging
and validating its replacement. Unrelated folders are never overwritten.
The prior package may belong to either supported method, allowing explicit
method changes without discarding unrelated workspace files.
The reproduction launcher accepts repeatable `--tutorial-input-asset` for real
textures or dependency folders; `--image` retains its acceptance-reference role.

Capability spelling is normalized to the project's established `GEO` label
(the supplied archive used `GE0`). No classification CSV is modified.
