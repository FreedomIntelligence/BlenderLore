---
name: blender-pipeline
description: Route Blender work among maintained-showcase reproduction, from-zero generation, and scoped editing of existing assets, with shared quality gates and maintained knowledge references.
---

# Blender Pipeline Router

Select exactly one primary mode before loading implementation detail.
## Public one-command tutorial replay

Use repository-root `run_codex.py` or `run_api.py`; see the root README for commands.
Video file/URL or `--tutorial` requires a new output directory outside the repo,
not RW1, private workers, or a showcase recipe. Pass source `--asset`, dependency
`--asset-root`, textures `--input-asset`, starting `--preview`, and final
`--target-image` explicitly. Source-assisted replay is not unrestricted editing.
Choose `--tutorial-method visual` (recommended ≤10 minutes) or `legacy-rich`
(original rich-window/base64 workflow). Optional `--render-html` adds no model
call. Root launchers do not automatically promote outputs into active knowledge.

## Extract a tutorial from video

Use tutorial extraction when the immediate deliverable is a complete illustrated
Markdown tutorial, learner inputs, and a separate 100-point JSON rubric. Read
[tutorial-extraction/SKILL.md](tutorial-extraction/SKILL.md). Generation and
reproduction delegate their video-understanding stage to this same entrypoint.

## Reproduce a maintained showcase

Use reproduction when the user names a cataloged showcase and supplies video,
tutorial, image, or authorized source-asset inputs. This hybrid route first
enforces recipe, visual-acceptance, and distribution gates, then delegates the
actual scene work to generation or editing.

Read [reproduction/SKILL.md](reproduction/SKILL.md). Do not treat a public
preview or catalog entry as asset-download authorization.

## Generate from zero

Use generation when the requested asset must be reconstructed from a video,
tutorial, reference sequence, or textual specification and no existing project
is the authoritative baseline.

Read [generation/SKILL.md](generation/SKILL.md), then load
[knowledge/generation.md](knowledge/generation.md),
[knowledge/evidence-and-routing.md](knowledge/evidence-and-routing.md), and only
the relevant sections of
[knowledge/rendering-and-dynamics.md](knowledge/rendering-and-dynamics.md).

## Edit an existing asset

Use editing when a `.blend` file, imported scene, or other existing asset is the
authoritative baseline and the request changes only selected materials, colors,
geometry, modeling, or scene elements. This
remains an editing task when the replacement object itself is generated from
scratch by the pipeline.

Read [editing/SKILL.md](editing/SKILL.md), then load
[knowledge/editing.md](knowledge/editing.md) and only the relevant sections of
[knowledge/rendering-and-dynamics.md](knowledge/rendering-and-dynamics.md).

## Shared rules

- Do not run the from-zero pipeline merely because an edit contains a newly
  generated replacement.
- Keep mode-specific code, tests, manifests, and outputs separate. Share only
  stable utilities and contracts.
- Preserve verified source intent and non-target dimensions. A successful file
  write is not a visual or semantic pass.
- Use explicit input/output paths and environment-backed credentials; never
  embed workstation, mounted-volume, or server paths in committed code.
- Load [knowledge/operations-and-knowledge.md](knowledge/operations-and-knowledge.md)
  only for orchestration, paid calls, knowledge maintenance, or publication.
- Start knowledge lookup at [knowledge/index.md](knowledge/index.md); do not
  load every reference for an ordinary single-mode task.

Validate the packaged skill with:

```bash
python3 skills/blender-pipeline/scripts/validate_package.py
```
