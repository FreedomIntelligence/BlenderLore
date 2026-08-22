# Evidence and route selection

These rules decide what the pipeline is allowed to build or edit before model
generation or Blender execution begins.

## Select the authoritative source before the pipeline mode

**Applies when:** A request includes any combination of video, tutorial text,
images, an existing project, or an imported asset.

**Rule:** An existing project that must be preserved is the editing baseline.
Video/tutorial evidence without an authoritative project routes to from-zero
generation. A newly generated replacement inside an existing project does not
change the route from editing to generation.

**Verify:** The task manifest names one primary mode, the authoritative source,
and which inputs are auxiliary. No mode silently rebuilds an input owned by the
other mode.

**Search terms:** `source authority`, `generation vs editing`, `hybrid edit`

## Build executable tutorial steps from stable evidence

**Applies when:** A video or screen recording is the source of modeling,
material, animation, or scene operations.

**Rule:** Turn each candidate operation into three questions: what changed,
what final value is visible, and which post-change frame proves it. Sample a
dense window around the cue. Use step start, best overall, stable post-change,
and OCR-best frames rather than one isolated screenshot. Only verified values
enter executable steps; unresolved values remain explicitly unverified.

**Verify:** Every executable operation has an evidence label, a stable frame or
transcript/UI basis, and an explicit final value. The tutorial contains textual
constraints for objects, materials, surface details, spatial relations, and
items that must not be omitted.

**Search terms:** `Q-Gate`, `stable frame`, `verified operation`, `OCR window`

## Treat UI OCR as parameter evidence, not subject authority

**Applies when:** OCR reads Blender labels, subtitles, numeric fields, node
names, or unrelated words from the interface.

**Rule:** UI numeric values, dropdowns, modifier names, timeline positions, and
node names are stronger parameter evidence than narration. Title and verified
steps determine the asset family. Use bilingual aliases and boundary-aware
matching; a token contained inside another word and explicitly negated phrases
must not create positive traits.

**Verify:** Classification cites title or verified operations, not an isolated
OCR token. Material words alone do not imply simulation, generic node words do
not imply a shader task, and a scene word inside unrelated text does not imply
an environment.

**Search terms:** `UI OCR`, `asset family`, `bilingual aliases`, `negation`

## Validate final-reference frames before visual use

**Applies when:** A pipeline extracts or receives a nominal final frame.

**Rule:** A final-reference frame is auxiliary until validated as the finished
asset. Title cards, course promotions, web/video UI, shader editors, and broad
Blender panel layouts cannot constrain generated geometry or composition.
Absence of OCR is not proof of validity; UI separators and properties/outliner
boundaries are also rejection signals.

**Verify:** The reference policy records valid, auxiliary, or rejected. If the
frame is not valid, visual matching falls back to ordered tutorial evidence and
verified steps, or the pipeline abstains when those sources do not establish a
finished target.

**Search terms:** `final reference`, `title card`, `Blender UI`, `auxiliary frame`

## Derive dynamic routes from subject motion

**Applies when:** A title, request, or classifier suggests animation, dynamics,
or a static route.

**Rule:** Dynamic requires verified object transform, deformation, material
animation, simulation, or explicitly required camera animation. Camera motion,
lighting changes, background animation, full-frame pixel difference, and words
such as inflation or erosion are insufficient by themselves. Static tutorials
must be allowed to stop after a static deliverable.

**Verify:** The route manifest cites motion mechanism and evidence across time.
If the delivered scene has no verified subject motion, route it to static
instead of inventing generic keyframes or a turntable.

**Search terms:** `dynamic route`, `subject-local motion`, `camera-only`, `static override`

## Scan long tutorials before committing to a dynamic contract

**Applies when:** Only a small number of windows represent a long source and a
dynamic deliverable is expected.

**Rule:** Run a deterministic scan of the remaining transcript/evidence for
keyframes, animated parameters, Scene Time, deformation, solving, or
simulation before model generation. Do not pay for or render a dynamic route
whose evidence coverage cannot establish the requested motion.

**Verify:** The route decision states the scanned coverage and the mechanism
found. An unresolved mismatch abstains before code generation rather than
silently degrading later.

**Search terms:** `evidence coverage`, `long tutorial`, `motion scan`, `abstain`

## Select linked projects by subject evidence

**Applies when:** A download or source bundle contains multiple projects,
add-ons, demos, resource packs, templates, or interchange files.

**Rule:** Select a finished source by page-specific title, subject, and artifact
evidence. Never choose the first, shortest, smallest, or lexicographically
named project. Add-on examples, startup files, material libraries, comparison
boards, and generic preview primitives are supply, not finished assets. Large
ambiguous bundles require an explicit unique match or abstention.

**Verify:** Selection records why the chosen project represents the requested
subject and why competing candidates were excluded. Only the selected artifact
and its bounded dependencies are staged.

**Search terms:** `linked project`, `source selection`, `resource pack`, `ambiguous bundle`

## Preserve episode and series dependencies

**Applies when:** Numbered or similarly titled tutorial episodes form a
continuous build.

**Rule:** Normalize the series prefix and require accepted predecessors before
later episodes run. Lighting-, material-, or camera-only episodes inherit the
completed prior geometry and materials; they are not independent white-model
reconstructions.

**Verify:** The manifest records predecessor order and the inherited artifact.
A later episode cannot prepare or render while a required predecessor is
missing or rejected.

**Search terms:** `episode dependency`, `series prefix`, `predecessor`, `inherit source`

## Model object relations only when evidence persists

**Applies when:** Similar silhouettes, color variants, LODs, before/after
states, or repeated parts might be mistaken for one assembly.

**Rule:** Treat same-silhouette alternatives as variants unless ordered
evidence shows persistent attachment or a verified operation performs
assembly/parenting. Repeated detail groups do not authorize hiding the main
body or inventing a parent-child composite.

**Verify:** Before generation, record subject instance count, variant groups,
physical hierarchy, major component checklist, and forbidden interpretations.
Uncertain relations abstain instead of being guessed.

**Search terms:** `variant group`, `physical hierarchy`, `multi-part subject`, `attachment`

## Apply user corrections as source truth

**Applies when:** The user corrects color, material, geometry, timing, object
identity, source selection, or another inferred fact.

**Rule:** The correction overrides OCR and model inference and becomes a
reusable constraint in the tutorial, route/edit manifest, and local review.
Keep source appearance and requested target appearance distinct.

**Verify:** Subsequent prompts, scripts, and reviews use the corrected fact;
stale descriptions do not survive in public task text or executable contracts.

**Search terms:** `user correction`, `source appearance`, `target appearance`, `hard truth`
