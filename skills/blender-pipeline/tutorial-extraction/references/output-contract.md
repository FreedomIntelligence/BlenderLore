# Visual tutorial and pipeline contract

The canonical `extract_video_tutorial.py` now runs `video-to-visual-tutorial`.
The supplied skill's learner package is retained:

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

The JSON rubric stays inside the subject package and is not read into the
Blender task prompt. Tutorial extraction only documents the workflow; it does
not execute the embedded verifier or build/render a solution.

`--render-html` adds `illustrated_tutorial.html` at the workspace root,
outside the subject package's `output/`, using the same Markdown without a
model call. The two-file learner-output contract is preserved. Generation's
`--render-tutorial-html` forwards to this behavior.

Analysis lives beside the workspace under `.video-tutorial-cache/` by default;
`--cache-dir` overrides it. The cache must be outside the tutorial workspace.
It stores timestamped frames, resolved ledgers and normalized model results to
resume retries. URL downloads are temporary. Provider secrets and raw transport
responses are not included in the learner package.

Legacy v2 package validation remains available for historical results.
The canonical CLI and new runs do not execute the v2
fragment extraction algorithm. A new standalone run needs an unused subject
output path. Explicit generation `--force-tutorial` forwards `--replace-existing`,
which replaces only the previous manifest-owned subject package after staging
and validating its replacement. Unrelated folders are never overwritten.
The reproduction launcher accepts repeatable `--tutorial-input-asset` for real
textures or dependency folders; `--image` retains its acceptance-reference role.

Capability spelling is normalized to the project's established `GEO` label
(the supplied archive used `GE0`). No classification CSV is modified.
