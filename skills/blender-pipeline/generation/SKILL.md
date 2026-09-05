---
name: blender-pipeline-generation
description: Generate an editable Blender asset or animation from a blank scene, either by replaying verified tutorial-video evidence or by executing a model-direct generation task. Use for from-zero reconstruction; use the edit pipeline when an existing scene or asset must be modified.
---

# Blender Generation Pipeline

Produce an editable `.blend` as the primary artifact. Treat preview renders,
six-view images, turntables, receipts, and traces as validation evidence rather
than substitutes for the scene.

## Choose a route

- Use tutorial replay when a video or tutorial is the source of truth. Prepare
  timestamped evidence, retrieve relevant knowledge, generate the scene, then
  compare it with the verified visual references.
- Use model-direct generation when a structured task and reference preview are
  already available. Begin from a canonical blank scene and run the bounded
  harness in `scripts/model_direct_generation_v3_harness.py`.
- Do not use either route to edit an existing asset. Route that work to the
  sibling edit pipeline.

## Configure the environment

Install `requirements.txt`; add `requirements-knowledge.txt` only when building
or querying the semantic knowledge index. Blender supplies `bpy`, `mathutils`,
and `bpy_extras`; never install those modules with pip. Install Blender and
FFmpeg separately. OCR evidence additionally uses the optional `tesseract`
executable.

Paths default below this skill's `output/` directory. Override them with
`VIDEO2BLENDER_OUTPUT_ROOT` and `BLENDER_VIDEO_ROOT`. Set `BLENDER_PIPELINE_BLENDER` to the Blender
executable. Paid model calls require an owner-only secret file via
`BLENDER_PIPELINE_API_KEY_FILE` and an explicit HTTPS endpoint via
`BLENDER_PIPELINE_API_ENDPOINT` and `VIDEO_REPLAY_APPROVED_PAID_API_ENDPOINT`. Do not
commit credentials, provider endpoints, machine mounts, or recovery authority
files. Deprecated `PAPER12_*` options are accepted as lower-priority aliases;
new integrations must use the `BLENDER_PIPELINE_*` prefix.

## Replay verified tutorial evidence

1. Put the source video and any transcript under one `video-dir`.
2. Read the sibling [tutorial extraction skill](../tutorial-extraction/SKILL.md)
   and run its canonical `extract_video_tutorial.py` entrypoint. It performs
   the `video-to-visual-tutorial` workflow: coarse contact sheets, focused
   frame inspection, a compact evidence ledger, and a complete learner-facing
   procedure. Run the entrypoint with `--workspace-mode` to adapt that package
   to the existing replay files. Pass actual learner assets with repeatable
   `--input-asset`; do not classify the source video or acceptance preview as
   a learner asset. The older generation
   helpers are compatibility internals and must not be invoked as a second
   extraction route. Run `build_pipeline_specs.py` after extraction.
   `tutorial.md` and its derived `steps_verified.json` preserve the operational
   contract. The separate rubric is never supplied as task instructions.
   Add `--render-tutorial-html` only when a separate human-readable
   HTML view is useful; it is rendered from the same complete Markdown and never
   replaces the operational files.
3. Run `run_video_replay_main.py --video-dir <dir>`. The orchestrator invokes
   the strict replay, version registry, render evidence, and candidate
   knowledge update stages.
4. Require an editable scene, a successful fresh render, complete six-view
   evidence, and a turntable or validated animation delivery when motion is
   actually supported by evidence.

Tutorial text and timestamped frames outrank titles and stylistic hints. Never
invent motion merely because a requested route says “dynamic”; downgrade to a
static result unless subject animation is proved by keyframes, Actions/NLA,
shape keys, time-dependent materials, simulations, or equivalent runtime
evidence. Preserve the demonstrated asset family, silhouette, detail density,
camera framing, lighting, and material response.

Two legacy route labels remain in receipts for compatibility. `RW1` means
source-project replay: preserve authored scene content and real subject motion,
using bounded replacement framing only when the authored camera is unusable.
`RW2` means a multi-part tutorial-series reconstruction: the tutorial and
verified steps are authoritative, and unrelated title-card or editor tail
frames must not become final visual references. Neither label denotes the
existing-asset edit pipeline.

## Run model-direct generation

Use `model_direct_generation_v3_harness.py create-blank` to establish the
canonical blank scene, then validate the exact-50 release task index.
`run-task` selects one item from that release index. The current `run-batch`
command rejects any other batch size and must not be presented as a general
batch runner. Supply explicit call and token budgets. Generated `generate.py`
must be self-contained Blender code: the contract rejects filesystem escape,
network access, subprocesses, dynamic imports, external project loading, and
private provenance. Each attempt is immutable and must reopen the saved blend,
render fresh evidence, and pass both the generation rubric and visual judge.

## Knowledge lifecycle

Build a portable manifest with
`build_blender_knowledge_index.py --manifest-only`; install the optional
knowledge dependencies to build Qdrant. Retrieve with
`retrieve_blender_knowledge.py --video-dir <dir>`. New replay observations are
candidate knowledge only. Promote a rule only after its evidence is durable,
its scope and Blender version are explicit, and an independent replay confirms
the outcome. Production evidence is ingested only through an explicit,
bounded `--external-manifest`; the builder never crawls local run outputs.
