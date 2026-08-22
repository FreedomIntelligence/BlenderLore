# Existing-asset editing

Use these rules when a source project or imported asset is the authoritative
baseline. The replacement may still be generated entirely by the pipeline.

## Preserve the source project as the baseline

**Applies when:** A complete project or supported interchange asset is supplied
for material, palette, geometry, modeling, or scene edits.

**Rule:** Open the source directly and preserve authored geometry, hierarchy,
materials, world, lights, compositor, camera, color management, animation, and
dependencies outside the approved mutation scope. Do not reconstruct the whole
scene from a video-generation path merely because one replacement is new.

**Verify:** The edit manifest names the source artifact and mutable dimensions.
Unmodified source dimensions remain equivalent in the result.

**Search terms:** `existing baseline`, `source project`, `scoped edit`, `authoritative asset`

## Require meaningful source integration

**Applies when:** An editing workflow links, imports, or opens an existing
project and also adds generated content.

**Rule:** At least one visible renderable source object must be deliberately
modified or explicitly retained as part of the final composition. Cameras,
lights, empties, hidden evidence objects, and generated supplemental objects do
not by themselves prove source use. As a conservative default, preserve at
least half of the source renderable objects unless the task explicitly replaces
more.

**Verify:** The result reports loaded, preserved, modified, hidden, and replaced
renderable objects, and direct review confirms that the source subject—not an
unrelated generated scene—remains present.

**Search terms:** `source integration`, `renderable preservation`, `linked asset`, `generated supplement`

## Declare a narrow mutation contract

**Applies when:** An edit can touch material, color, shader, geometry, modeling,
or scene dimensions while animation remains protected source state.

**Rule:** State allowed objects/data blocks, editable properties, protected
dimensions, and required output before executing code. Editing an unused
datablock does not satisfy a visible edit. Changes outside the contract fail
even when they improve the image.

**Verify:** Before/after scene diagnostics identify changed objects, materials,
nodes, transforms, and settings. Every change maps to an allowed dimension.

**Search terms:** `mutation scope`, `protected dimension`, `visible datablock`, `change set`

## Keep generated replacements inside the original visual envelope

**Applies when:** An object or geometry is replaced rather than recolored.

**Rule:** Unless the task explicitly requests scale change, fit the replacement
to the original world-space center, orientation, visible dimensions, and
approximate volume. Preserve the dominant visual mass and surrounding negative
space even when the replacement changes object category.

**Verify:** Record source and target bounding boxes, centers, per-axis extent
ratios, and volume ratio. Confirm the rendered subject is neither visibly
shrunk nor enlarged and remains uncropped from the frozen camera.

**Search terms:** `replacement envelope`, `bounding box`, `volume ratio`, `world transform`

## Separate source appearance from target appearance

**Applies when:** A task changes color, material family, geometry family, or
object identity.

**Rule:** Detect and record a concrete `from_type` from the visible source and a
concrete `to_type` from the requested edit. Do not rewrite the baseline
description to match the target. Empty, identical, or category-only labels are
not executable edit contracts.

**Verify:** Request text, recipe, diff, and result status use the same distinct
source-to-target transition, including corrected source colors or materials.

**Search terms:** `from_type`, `to_type`, `source color`, `target material`

## Preserve active material graphs and dependencies

**Applies when:** A material, shader, palette, or surface family is edited.

**Rule:** Trace every socket path reaching visible Material Output nodes,
including node groups, textures, emission, volume, and legacy shaders. Do not
clear the node tree or delete source images/groups/dependencies unless deletion
is explicitly in scope. Add uniquely named edit nodes and replace only the
necessary active connection or values.

**Verify:** The intended transition is visible on assigned renderable meshes,
source texture/detail paths that remain required still resolve, and socket
values match runtime types and arity.

**Search terms:** `material graph`, `Material Output`, `nodes.clear`, `socket arity`

## Reject material edits that erase structural appearance

**Applies when:** Shader structure supplies identity-defining details, decals,
procedural silhouettes, volumes, eyes, lashes, or other apparent geometry.

**Rule:** A graph-level material replacement is ineligible when it destroys
essential subject structure. Select a source whose geometry survives the
material change or narrow the edit to preserve those graph branches. A valid
node receipt cannot override a visibly erased asset.

**Verify:** Before/after comparison retains identity-defining details and
silhouette while making the target material obvious.

**Search terms:** `shader structure`, `procedural silhouette`, `identity detail`, `material eligibility`

## Keep material-only edits readable without lighting changes

**Applies when:** The edit scope excludes lighting and the source has weak or
minimal authored lights.

**Rule:** Choose material parameters that remain readable under the frozen
presentation. Avoid near-black fully metallic targets; use practical base
color, nonzero diffuse response, metallic below its extreme, and moderate
roughness unless the requested material requires otherwise. Do not alter lights
to rescue an unreadable material-only edit.

**Verify:** Foreground luminance, dark fraction, and visible-delta gates pass
with unchanged lighting, camera, color management, and exposure.

**Search terms:** `material readability`, `frozen lighting`, `dark fraction`, `visible delta`

## Render controlled before-and-after pairs

**Applies when:** An edit is judged by comparison.

**Rule:** Render both sides from the same source-scene snapshot with identical
camera, visible-object set, color management, exposure, resolution, frame time,
background, and renderer policy. A historical preview with different
presentation is provenance only, not a valid before image.

**Verify:** Comparison metadata proves presentation parity and shows that only
the declared edit dimension changed. Foreground masks remain comparable.

**Search terms:** `before after`, `frozen presentation`, `comparison parity`, `same frame`

## Prefer authored presentation for source-faithful delivery

**Applies when:** The source scene already has a usable engine, camera, lights,
world, compositor, color management, exposure, and animation timeline.

**Rule:** Preserve that authored presentation and override only the explicit
delivery resolution, frame sampling, or bounded sample budget. Do not force a
different render engine, add a replacement camera, or run generic adaptive
lighting when the source presentation is valid. Interchange assets without
presentation settings may use a clearly labeled conservative fallback.

**Verify:** The native source remains unchanged, delivery metadata identifies
whether authored or fallback presentation was used, and fallback output passes
stricter direct review.

**Search terms:** `authored presentation`, `source-faithful`, `render engine`, `fallback camera`

## Frame animated sources over their temporal bounds

**Applies when:** A static edit is applied to an animated source.

**Rule:** Sample a bounded deterministic set across the source timeline and
frame the union of subject bounds, then restore the saved frame. Do not compute
the edit camera from one arbitrary current frame that clips later motion.

**Verify:** Before and after use the same temporal presentation, and sampled
frames keep the subject within frame without changing source animation.

**Search terms:** `animated source`, `temporal bounds`, `camera union`, `frame restore`

## Preserve semantic scene geometry while hiding helpers

**Applies when:** Source projects contain floors, walls, broad planes,
reference boards, swatches, labels, grids, or tutorial controls.

**Rule:** Floors, walls, ceilings, windows, room shells, and volume environments
are semantic source geometry. Hide only objects proven to be detached
reference, comparison, swatch, annotation, preview, or construction helpers.
Generic primitives and sparse diagrams without an independently useful modeled
subject are ineligible edit baselines. Hidden custom properties used for
provenance are metadata, not visible helpers and must not cause source geometry
to be removed.

**Verify:** The edit records selected and hidden object roles. The final render
contains the actual subject and authored final-scene props, not resource-library
grids or teaching panels.

**Search terms:** `presentation helper`, `room shell`, `swatch board`, `source eligibility`

## Apply category-specific visible-delta gates

**Applies when:** The mutation is semantically valid but may be visually weak.

**Rule:** Lighting edits must change luminance or warm/cool balance; palette,
material, and shader edits must change a meaningful foreground fraction and
appearance distance; surface-detail edits must also change visible edge/detail
energy. Lighting changes remain bounded and relative to authored lamps rather
than stacking generic rigs.

**Verify:** The requested dimension is obvious at first glance while geometry,
camera, composition, exposure, and other protected dimensions remain stable.

**Search terms:** `category delta`, `lighting edit`, `palette distance`, `detail energy`

## Maintain aesthetic non-regression

**Applies when:** A replacement can technically satisfy the edit while making
the composition less coherent or attractive.

**Rule:** Match the source's visual hierarchy, scale, balance, material depth,
lighting compatibility, and finish quality. Replacement variants may change
material or object family, but each must be deliberately composed rather than
being a random primitive or default shader.

**Verify:** Direct before/after review confirms the target edit is visible and
the result is not worse in framing, completeness, readability, artifacts, or
overall presentation.

**Search terms:** `aesthetic non-regression`, `visual hierarchy`, `finish quality`, `replacement quality`

## Reuse generated assets without weakening provenance

**Applies when:** A previously pipeline-generated object or material variant is
reused as the replacement in a new edit task.

**Rule:** Reuse is allowed when the replacement remains fully pipeline-generated
and its source, license, geometry/material identity, and compatibility are
known. Adapt only transform and explicitly editable presentation properties;
do not claim an externally sourced asset was generated.

**Verify:** The edit manifest points to the generated asset identity and records
any fit operation. Multiple variants are materially or geometrically distinct,
complete, and not duplicate recolors mislabeled as new objects.

**Search terms:** `generated replacement`, `asset reuse`, `variant distinctness`, `fit operation`

## Validate native and interchange deliverables separately

**Applies when:** The result requires both a native scene and a derived format
such as GLB.

**Rule:** The native scene is the editing authority. Export derived formats only
after the native result passes; preserve supported materials, transforms,
geometry, animation, and texture references within the target format's limits.
Never substitute a different nearby asset merely because it exports cleanly.

**Verify:** Reopen or inspect every required deliverable, confirm nonzero
renderable content and source identity, and ensure the exported subject matches
the approved native render and expected path contract.

**Search terms:** `GLB export`, `derived format`, `source identity`, `reopen validation`

## Let the harness own result publication

**Applies when:** Edit code is executed by a benchmark or production harness.

**Rule:** Generated edit code mutates only the declared in-memory scene. The
harness saves the result, renders before/after evidence, validates output paths,
and publishes atomically. Edit code must not call broad file operators or write
outside its assigned result directory.

**Verify:** Static inspection rejects unauthorized file operations, and the
harness can reproduce the same result from the same baseline and edit request.

**Search terms:** `edit harness`, `result publication`, `file operator`, `reproducible edit`
