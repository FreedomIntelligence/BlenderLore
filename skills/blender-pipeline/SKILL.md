---
name: blender-pipeline
description: Extract a tutorial or reconstruct a Blender result from video links, local videos with supporting assets, or Markdown tutorials in Codex chat. Clarify the intended output when unspecified; also route explicit showcase reproduction and independent asset edits.
---

# Blender Pipeline

## Start from the Codex client

This is the user-facing Codex launcher. If the requested output is unclear, ask
whether the user wants only a tutorial or a complete Blender reconstruction;
wait for their choice before launching. Do not ask again when the goal is explicit.

Read [references/codex-launch.md](references/codex-launch.md), interpret the
user's natural-language inputs, and invoke this skill's
`scripts/launch_from_codex.py` yourself. It calls the same maintained pipeline
as the API entrypoint. Tutorial-only stops after preparation; full reconstruction
continues through knowledge retrieval → Blender generation → render and review.

A video with an accompanying `.blend`, preview, texture, or dependency bundle
retains those inputs in either selected route; it is not an independent edit.
A supplied Markdown tutorial skips video extraction: prepare it for tutorial-only,
or continue knowledge-backed generation for reconstruction. No RW1 is required.

Use the user's requested output location or established data root. Ask only for
missing information needed to run, not command-line flags or API credentials.
The Codex path uses the signed-in Codex CLI; the user need not run a Python
launcher manually. `run_codex.py` remains a compatibility entrypoint, not the
client interaction. Requests to explain, inspect, or dry-run do not authorize
an actual reconstruction.

## Extract a tutorial from video

For tutorial-only, pass `--extract-only` and do not start reconstruction. Read
[tutorial-extraction/SKILL.md](tutorial-extraction/SKILL.md) for illustrated
Markdown, learner inputs and the separate rubric; both replay routes reuse it.

## Reproduce a maintained showcase

Read [reproduction/SKILL.md](reproduction/SKILL.md) only for an explicitly
requested catalog recipe or hybrid-delivery workflow. A tutorial's subject
also appearing on the showcase does not change ordinary replay routing.
Public previews do not authorize downloading the underlying assets.

## Generate from zero

For ordinary video/tutorial requests use the client launcher above. Use the
specialized generation mode for an explicit from-zero reference/text task or
low-level generation work, with no authoritative starting project.

Read [generation/SKILL.md](generation/SKILL.md), then load
[knowledge/generation.md](knowledge/generation.md),
[knowledge/evidence-and-routing.md](knowledge/evidence-and-routing.md), and only
the relevant sections of
[knowledge/rendering-and-dynamics.md](knowledge/rendering-and-dynamics.md).

## Edit an existing asset

Use editing for a standalone change request on an existing asset, without a
video/tutorial reconstruction workflow. A generated replacement object within
that scoped change remains part of the edit.

Read [editing/SKILL.md](editing/SKILL.md), then load
[knowledge/editing.md](knowledge/editing.md) and only the relevant sections of
[knowledge/rendering-and-dynamics.md](knowledge/rendering-and-dynamics.md).

## Shared rules

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

Package maintenance uses `scripts/validate_package.py`, not every ordinary launch.
