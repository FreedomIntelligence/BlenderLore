# From-zero generation

Use these rules only when no existing project is the authoritative baseline.

## Freeze a constraint manifest before code generation

**Applies when:** Verified tutorial or reference evidence is ready to become
Blender code.

**Rule:** Build a machine-readable constraint manifest from ordered evidence,
verified operations, route, required outputs, allowed assumptions, and
non-regression constraints. The tutorial's step sequence is the execution
spine; API compatibility guidance only constrains implementation.

**Verify:** Generated plans contain every locked operation and parameter, name
unverified facts rather than filling them in, and cannot weaken the manifest.

**Search terms:** `constraint manifest`, `execution spine`, `locked parameter`, `non-regression`

## Preserve operation fidelity before visual approximation

**Applies when:** The source demonstrates a concrete Blender mechanism such as
particles, nodes, modifiers, keyframes, cloth, rigid bodies, or hair dynamics.

**Rule:** Implement the verified mechanism and values before optimizing visual
similarity. Do not replace native systems with look-alike curves, meshes,
procedural stand-ins, or category-level approximations unless the user
explicitly accepts that fallback after a runtime incompatibility.

**Verify:** Local review checks the required Blender data types, operators,
nodes, properties, operation order, and values—not merely output filenames or
render resemblance. Unmentioned parameters retain supported defaults.

**Search terms:** `operation fidelity`, `native mechanism`, `verified parameter`, `fallback`

## Generate in isolated executable stages

**Applies when:** A task is complex enough that one rewrite could regress an
already correct dimension.

**Rule:** Separate scaffold, geometry, material, composition/camera,
lighting/render, and physics/animation. Each stage declares allowed and
forbidden edits, produces an inspectable checkpoint, and starts repair from
the last approved checkpoint. A wrapper that only recopies seed code is not a
stage implementation.

**Verify:** Stage review states pass/fail items, regressions, and next action.
A patch touching a forbidden dimension fails even when the new render looks
better. Mixed outcomes such as static-pass/dynamic-fail remain explicit.

**Search terms:** `stage isolation`, `checkpoint`, `allowed edits`, `SEIG`

## Respect harness ownership of files and rendering

**Applies when:** Generated code runs inside a harness that opens a baseline,
saves the result, renders evidence, or controls output paths.

**Rule:** The harness and generated code must have disjoint ownership. In the
maintained direct-generation contract, generated code may create and edit only
geometry and materials; it cannot use the network, read or write files, load
external assets, or control camera, lights, world, rendering, and saving. The
harness owns those scene/presentation dimensions. A standalone script performs
its declared save/render/encode operations only when no harness owns them.

**Verify:** Completeness checks inspect executable calls and entrypoints, not
filenames embedded in strings. The baseline cannot be overwritten by generated
code.

**Search terms:** `harness ownership`, `save_as_mainfile`, `output contract`, `baseline overwrite`

## Execute from a canonical blank and verify a fresh reopen

**Applies when:** Direct generation claims to create the complete asset without
an existing scene baseline.

**Rule:** Start from the versioned canonical blank scene rather than ambient
startup state. After generated geometry/material code completes, let the
harness save, close, and freshly reopen the result before presentation or
evaluation. In-memory success cannot substitute for reopenability.

**Verify:** The input blank identity is known; reopened scene diagnostics match
the saved result; no hidden linked files, startup objects, or process-local data
are required.

**Search terms:** `canonical blank`, `fresh reopen`, `startup state`, `scene portability`

## Freeze a complete visual contract

**Applies when:** Evidence or a task specification is converted into direct
generation constraints.

**Rule:** The visual contract explicitly records the hero-facing orientation,
must-have components, silhouette, material behavior, spatial relations, and
forbidden traits. A category label or prose summary is not enough to prevent a
generic but incorrect asset.

**Verify:** Every required component and forbidden trait has a deterministic or
visual check, and camera-facing details are visible from the declared hero
orientation.

**Search terms:** `visual contract`, `hero face`, `must-have`, `forbidden traits`

## Evaluate generated assets through complementary views

**Applies when:** A direct-generation candidate reaches formal review.

**Rule:** Reopen the native scene and render the maintained evidence suite: a
finished presentation view, clay view, normal view, and eight deterministic
coverage views. Presentation judges appearance; clay and normal views expose
geometry; coverage views expose missing or hidden components. Do not alter the
asset between views.

**Verify:** All views come from one reopened scene and one geometry/material
state, with view-specific presentation only. Missing views or inconsistent
content fail the attempt.

**Search terms:** `presentation view`, `clay view`, `normal view`, `eight views`

## Bound formal generation attempts and joint review

**Applies when:** A direct-generation task enters formal iterative evaluation.

**Rule:** Allow at most three immutable attempts. Each attempt retains its
input contract, generated code, reopened scene, view suite, deterministic
rubric, and VLM review. Acceptance requires both hard contract gates and a
Rubric-plus-VLM joint score of at least 80; a later attempt never overwrites an
earlier one.

**Verify:** Attempt numbering is monotonic, reviewer outputs refer to the exact
view suite, and the selected result satisfies the threshold without averaging
away a hard missing-component or forbidden-trait failure.

**Search terms:** `immutable attempt`, `joint score`, `Rubric`, `VLM review`

## Gate static quality before preview and full animation

**Applies when:** A generation route may produce animation.

**Rule:** Compile first, verify output ownership, render the static target, then
render a short continuous preview, and only then render the full animation.
Stop early on shape, material, camera, silhouette, or composition failure.

**Verify:** Each gate consumes a clean output location and a last-known-good
checkpoint. Full rendering cannot begin merely because Blender exited
successfully or files exist.

**Search terms:** `static gate`, `preview gate`, `full gate`, `early stop`

## Prevent batch template collapse

**Applies when:** Multiple tutorials or asset families are generated in a
queue.

**Rule:** A title/contact-sheet-to-generic-template shortcut is a smoke path,
not production. Every production item retains tutorial text, evidence windows,
verified steps, execution plan, constraint manifest, local review, and staged
renders. Category names alone do not define subject hierarchy, distinctive
parts, material cues, or composition.

**Verify:** Start a pipeline revision with a small cross-family quality set.
Similar-looking outputs trigger inspection of evidence coverage and generated
code family before scaling, even when file hashes differ.

**Search terms:** `template collapse`, `batch replay`, `cross-family`, `smoke path`

## Make semantic repairs narrow and evidence-backed

**Applies when:** Local or semantic review finds a mismatch after a usable
checkpoint exists.

**Rule:** Use deterministic diagnostics first. Camera, exposure, framing,
presentation, or runtime defects reuse the frozen semantic bundle. Rebuild
tutorial/code only for a proven subject or operation error, and propagate the
finding across all relevant evidence windows. Do not ask a model to rewrite
the whole script for a single visible defect.

**Verify:** The repair changes only the failed layer, retains prior visible
output until replacement passes, and reruns the same gate that detected the
failure.

**Search terms:** `targeted repair`, `frozen bundle`, `semantic defect`, `single variable`

## Preserve the complete evidence-backed assembly

**Applies when:** A subject contains multiple major parts or repeated detail
clusters.

**Rule:** Mark or enumerate all renderable focus parts. A detail cluster such as
wheels, petals, droplets, or engines cannot become the whole subject; nearby
body, silhouette, and identity-defining geometry remain in frame. Comparison
variants and teaching helpers remain excluded unless evidence makes them part
of the final assembly.

**Verify:** The rendered artifact passes a major-component checklist extracted
from multiple source windows, not only the last or most effect-heavy frame.

**Search terms:** `focus set`, `major components`, `repeated detail`, `complete assembly`

## Match presentation complexity to the task contract

**Applies when:** The generated result is either a single isolated object or a
simple multi-object scene.

**Rule:** A single-object contract cannot add a studio set, floor, decorative
props, or background geometry as generated scene content. A simple-scene
contract must include and frame every specified component; it cannot collapse
to one hero prop plus omitted surroundings. Harness-owned lighting and world do
not count as generated scene geometry.

**Verify:** Reopened object inventory matches the declared complexity, and all
simple-scene components appear together in the evidence views without crop.

**Search terms:** `single object`, `no ground`, `simple scene`, `all components`

## Keep presentation separate from generated asset facts

**Applies when:** A final showcase, six-view diagnostic, turntable, or studio
render is added after asset generation.

**Rule:** Presentation may change camera motion, framing, background, floor,
and lighting within the delivery contract; it may not change verified
geometry, materials, hierarchy, or source animation. Use composed-scene,
character-loop, detail-showcase, or static-prop presentation according to the
subject and verified motion.

**Verify:** Diagnostic views remain asset-focused, while showcase output is
labeled as presentation. Static assets receive restrained camera treatment,
not invented subject animation.

**Search terms:** `presentation layer`, `six-view`, `showcase`, `turntable`

## Apply deterministic Blender compatibility rewrites

**Applies when:** Generated code targets a supported Blender version whose API
differs from the source-era API.

**Rule:** Known syntactic API migrations are deterministic preprocessing, not
new semantic generation. Examples include moving viewport pivot assignment to
scene tool settings in current Blender and removing unsupported legacy texture
properties while preserving supported scale, depth, basis, and coordinates.
In Blender 5.1, convert trusted sRGB color inputs to scene-linear values through
the harness helper instead of embedding unreviewed gamma arithmetic in generated
code.

**Verify:** Compatibility rewrites are version-gated, minimal, recorded by
rule name, and followed by compile/runtime checks. They never authorize a new
model call or alter the tutorial's intended operation.

**Search terms:** `Blender compatibility`, `pivot point`, `legacy texture`, `deterministic rewrite`

## Complete only the active route contract

**Applies when:** Static and dynamic routes have different deliverables.

**Rule:** Static requires its scene and still render; dynamic requires its scene
and validated video. Do not fail a valid dynamic result for missing static-only
diagnostics or a valid static result for missing animation. A terminal before
rendering explicitly reports that no artifact was produced and never
fabricates media.

**Verify:** Status, filenames, and artifact checks agree with the frozen route.
An empty review issue or contradictory route is corrected as a control-plane
defect without regenerating the asset.

**Search terms:** `route-specific deliverable`, `static output`, `dynamic output`, `artifact availability`
