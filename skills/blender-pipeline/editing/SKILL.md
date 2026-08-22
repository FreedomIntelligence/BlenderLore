---
name: blender-existing-asset-editing
description: Edit a declared part of an existing Blender project while preserving its authored composition, verifying non-target stability, and producing a reopen-verified receipt. Use for material, color, geometry, modeling, or scene edits; use a generation workflow when the whole asset must be created from zero.
---

# Blender Existing-Asset Editing

Use this workflow when the source is an existing `.blend` project and only a declared scope should change. The source project does not need to have been produced by a generation pipeline. Any replacement asset introduced by the edit must come from the authorized generation process or from declarative geometry in the reviewed request; this package never executes arbitrary request code.

## Runtime boundary

Install the ordinary Python package from this directory:

```bash
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
```

`bpy` is deliberately not a pip dependency. Run project inspection and mutation with the Python bundled in Blender. Pass every source, request, and output path explicitly; do not put workstation or server paths into code or receipts.

## Required workflow

1. Create an edit request conforming to `schemas/edit_request.schema.json`. Bind it to the exact source file SHA-256 and name every target.
2. Run preflight before mutation. Reject a source hash mismatch, absent targets, linked read-only targets, missing external dependencies, or a request that overlaps its output.
3. Capture a deterministic source snapshot. Apply exactly one edit kind: `material`, `color`, `geometry`, `modeling`, or `scene`.
4. Preserve authored structure outside the declared scope. Material edits retain the node tree and change only declared materials. Geometry edits retain object identity, transforms, collections, and material slots. Modeling may add only explicitly named generated objects or approved modifiers. Scene edits touch only declared lights, camera, world, or exposure fields. Snapshots expose constraints, drivers, NLA, compositor state, and important render/color settings so undeclared drift fails closed.
5. Geometry may use declarative vertices/faces or a generation handoff. A handoff must bind an accepted GEN-V3 attempt receipt by file SHA and receipt self-hash, bind the receipt's exact `asset.blend` by SHA/size, and name one mesh object. The evaluated donor mesh is fitted into the target envelope while the target object and its material slots remain authoritative. Compare the target's evaluated visible world mesh before and after at the evidence frame; record and verify transform/orientation, world envelope center, per-axis envelope ratios, and bounding-box volume ratio. Geometry targets with shape keys, animation/drivers/NLA, constraints, armature or simulation dependencies, instancing, or mismatched viewport/render modifier settings are outside this single-frame review contract and fail closed.
6. Run `prepare`: render before, apply, pack, validate non-target drift, save and reopen `candidate.blend`, render after with the same camera/settings, and atomically publish an `awaiting_review` package. Structural success is not completion.
7. Obtain an external human or VLM assessment conforming to `schemas/assessment.schema.json`. It must bind `review_package.json`, `candidate.blend`, `before.png`, and `after.png` by SHA-256, score both versions, keep `after >= before`, and meet the request threshold (at least 80).
8. Run `finalize`: verify every binding, reopen the assessed candidate, match its saved snapshot, then atomically publish `asset.blend`, evidence, assessment, and a non-null-visual `complete` receipt. A failed or unreviewed run must not publish a completed result.

Do not clear a material node tree to perform a local edit. Do not silently resize a replacement: geometry replacement is fitted to the source local bounding box by default and checked against declared envelope, volume-ratio, and world-center tolerances. Inline replacements must rebuild every source UV layer; generation handoffs must retain those layer names. Unsupported shape keys or color attributes fail preflight instead of being discarded. Visual evidence and external review are mandatory for completion; `evidence.render=false` is invalid.

## Commands

Validate a request outside Blender:

```bash
python -m blender_edit_pipeline.pipeline validate-request --request edit.json
```

Preflight, prepare, and finalize inside Blender (arguments after `--` belong to the pipeline):

```bash
blender source.blend --background --python src/blender_edit_pipeline/pipeline.py -- preflight --request edit.json --output source_map.json
blender source.blend --background --python src/blender_edit_pipeline/pipeline.py -- prepare --request edit.json --review-dir review
blender --background --python src/blender_edit_pipeline/pipeline.py -- finalize --review-dir review --assessment assessment.json --output-dir edited
```

`apply --output-dir review` remains a compatibility alias for `prepare`; it produces `awaiting_review`, never `complete`. Use `--overwrite` only when replacement of an existing review or completed output was explicitly authorized. The implementation keeps a backup until the new directory is in place.

## Request modes

- `material`: set declared Principled-BSDF inputs without destroying the graph.
- `color`: set Base Color or insert a Hue/Saturation node at the declared material input.
- `geometry`: replace mesh data from declared vertices/faces or one SHA-bound accepted generation attempt, retaining target identity, material slots, transform, and fitted envelope.
- `modeling`: add allowlisted primitives or modifiers declared in the request.
- `scene`: edit declared lights, a declared camera, world color, or exposure only when those scopes are explicitly enabled.

Treat an edit as complete only when `finalize` emits `status: "complete"`, `visual_evidence` contains the bound external assessment with non-regressing scores, `non_target_drift` is empty, `reopen.verified` is true, and every published artifact hash matches. An `awaiting_review` package is deliberately incomplete.
