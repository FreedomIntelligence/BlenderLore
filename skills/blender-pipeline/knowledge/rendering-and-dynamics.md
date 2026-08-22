# Rendering, visual QA, and dynamics

Use route- and subject-aware gates. Numeric thresholds are contract defaults,
not permission to override verified geometry or materials.

## Combine deterministic diagnostics with semantic review

**Applies when:** A static render, preview, animation, or before/after pair is
being accepted or repaired.

**Rule:** Export scene diagnostics and deterministic image/motion metrics before
semantic review. Measure object/material counts, camera and frame range,
keyframes, foreground coverage, bounding-box occupancy, luminance, clipping,
color family, edge contact, and sampled motion. Automated probes and direct
visual review remain separately labeled.

**Verify:** Numeric failures enter a targeted repair decision; semantic review
cannot waive a hard measurable failure or weaken locked constraints.

**Search terms:** `deterministic QA`, `scene diagnostics`, `semantic review`, `hard gate`

## Evaluate foreground separately from the canvas

**Applies when:** The output uses dark, black, transparent, or bright studio
backgrounds.

**Rule:** Whole-frame mean alone cannot judge subject exposure. Use alpha- or
mask-aware foreground mean, dark fraction, highlight clipping, and background
mean together. A bright subject on black is not underexposed; a tiny subject on
an almost black canvas is not rescued by its foreground mean.

**Verify:** Exposure decisions include both subject and canvas measurements and
state which contract applies. Achromatic targets may be exempt from color gates
but not framing or luminance gates. Conversely, low-chroma output remains in
review when verified evidence requires visible texture or color variation.

**Search terms:** `foreground luminance`, `black canvas`, `alpha-aware`, `dark fraction`

## Use contract-specific presentation guardrails

**Applies when:** A neutral opaque studio product or sparse static subject is
required.

**Rule:** For the maintained opaque-studio contract, transparent fraction must
not exceed 1%, whole-frame mean should be at least 55/255, and border/background
mean at least 45/255. A static frame below 25/255 whole-frame mean needs review.
If a broad helper/backdrop occupies more than 20% of any image edge, reject it;
if foreground coverage is below 8%, require at least 30% bounding-box occupancy.
Apply edge-contact and canvas-luminance checks to sampled dynamic frames as well
as static views.

**Verify:** Thresholds are applied only to the named contract and are rerun
after repair. Direct review still checks clipping, composition, and subject
identity.

**Search terms:** `studio guardrail`, `edge contact`, `foreground coverage`, `bbox occupancy`

## Adapt exposure from measured subject luminance

**Applies when:** Source-faithful output is too dark or clips highlights.

**Rule:** Start neutral and compute a bounded exposure adjustment from
foreground luminance. Cap it by a configurable adjustment limit and a 0.92
foreground-highlight ceiling. Use exposure-only repair for clipped white
subjects and one bounded non-additive light/exposure repair for dark subjects.
Never apply one fixed exposure to every scene or change materials as an
exposure workaround.

**Verify:** Persist pre/post foreground mean and maximum, adjustment, and
highlight fraction; rerender and pass the same gate.

**Search terms:** `adaptive exposure`, `highlight ceiling`, `subject mean`, `bounded repair`

## Treat visual differences as diagnostics, not proof

**Applies when:** Pixel differences, histogram shifts, or different cameras are
used to compare outputs.

**Rule:** Whole-frame change is a recall signal. Camera, background, exposure,
and encoding can dominate it, while small moving controls may be missed.
Diagnostic and delivery cameras may differ, so do not reject solely because
their histograms or masks differ. Acceptance depends on the route-specific
delivery artifact and requested semantic change.

**Verify:** Motion is subject-local across multiple times; editing delta is
computed from presentation-matched pairs; direct output review confirms the
intended visible effect.

**Search terms:** `pixel difference`, `subject-local`, `histogram`, `diagnostic camera`

## Frame the complete motion envelope

**Applies when:** An object, deformation, or simulation changes position or
shape over time.

**Rule:** Use a fixed camera or non-animated midpoint target that contains the
full verified motion envelope unless the tutorial explicitly requires tracking.
A camera that follows the subject can hide translation. Choose rotation axes
relative to the camera so requested side-to-side motion remains visible.

**Verify:** Sampled frames keep the subject in frame and visibly express the
requested motion; first-frame centering alone is insufficient.

**Search terms:** `motion envelope`, `camera tracking`, `midpoint target`, `visible axis`

## Validate simulation setup, evaluation, and outcome separately

**Applies when:** Hair, cloth, fluid, soft-body, rigid-body, or particle
dynamics are required.

**Rule:** Check mechanism enablement, keyframes/forces, cache range, operation
order, evaluation or playback, bake completion, and visible post-impulse
behavior as separate facts. Create dependent systems after required modifiers
and opt them into the modifier stack when the API requires it. Do not infer
runtime order from function-definition text; inspect the executed call order.

**Verify:** Logs show bounded progress before and after bake, caches are created
after motion inputs, and preview frames show the expected deformation, lag, or
settling rather than only file existence.

**Search terms:** `simulation bake`, `modifier stack`, `operation order`, `visible dynamics`

## Sample both impulse and relaxation

**Applies when:** Motion contains an impulse, keyframed displacement, hold,
fast segment, or physical settling.

**Rule:** Convert timing language into an explicit frame table with phase,
duration, distance, and speed. Preview continuous frames covering the impulse
and later relaxation. Do not use a very late unstable frame as the hero render
or stretch a pause into slow interpolation.

**Verify:** A velocity table matches the requested phase semantics, preview
frames are unique and continuous, and a stable target/post-simulation frame is
chosen intentionally.

**Search terms:** `frame table`, `impulse`, `relaxation`, `velocity audit`

## Bound qualitative motion relative to subject size

**Applies when:** The source says move, lift, swing, or offset “a little” without
a verified numeric value.

**Rule:** Estimate from evidence and otherwise use a conservative fraction of
the subject's dimensions. Excessive displacement that turns fur, cloth, or
fluid into curtains, spikes, sheets, or detached masses is a motion-amplitude
or framing defect before it is a material/geometry defect.

**Verify:** Compare displacement to the subject bounding box and change one
dynamics variable at a time from the last good static checkpoint.

**Search terms:** `qualitative motion`, `relative displacement`, `simulation collapse`, `amplitude`

## Keep hair and fur roots, shading, and motion coherent

**Applies when:** Native particle hair or fur is the verified mechanism.

**Rule:** Require dense attached roots, curved/clumped strands, covered base
surface, root-to-tip material depth, and visible tip lag. Keyframe the emitter
to move the whole object; do not animate only detached strands. Connect the
hair shader to the active material output and preserve the verified ordering of
motion evaluation, bake, camera, and material setup.

**Verify:** Static views reject radial needles, grass, ribbons, exposed smooth
base, and pasted brush shapes. Dynamic samples show root attachment and tip
response while the object remains framed.

**Search terms:** `particle hair`, `attached roots`, `tip lag`, `hair shader`

## Encode animation from complete sequential frames

**Applies when:** Blender renders frames that are assembled into video.

**Rule:** Render enough continuous unique frames at the intended cadence and
encode from sequential filenames. Changing only encoder frame rate does not
create smoother motion. Dense fast-moving detail needs high-quality encoding;
all-intra H.264 or an equivalent high-bitrate codec avoids motion-block and
color artifacts. Disable motion blur unless requested.

**Verify:** Wait for Blender and encoder process exit or stable final size, then
use media probing to validate duration, frame rate, dimensions, and decode.

**Search terms:** `sequential frames`, `ffprobe`, `all-intra`, `encoding quality`

## Keep render-engine policy consistent

**Applies when:** Generation, editing comparison, postprocess, or retry uses
Cycles or EEVEE.

**Rule:** All stages of one comparison consume the same verified engine/device
policy. Detect silent CPU fallback, wrong GPU selection, and black EEVEE probes
as infrastructure failures. For a compositor-only crash after completed Cycles
sampling, one bounded retry may disable compositing only if both before and
after use the same fallback; unrelated exits do not authorize it.

**Verify:** Record renderer, device identity/capability, color management,
compositor policy, and final process/media completion. Engine failures do not
trigger semantic code repair.

**Search terms:** `Cycles device`, `EEVEE probe`, `compositor fallback`, `engine parity`

## Fail on unresolved source dependencies

**Applies when:** A source project uses linked libraries, images, plug-ins,
caches, custom shaders, or packed data.

**Rule:** Missing dependencies block source-faithful publication. Do not replace
them with generic materials, lighting, geometry, or studio presentation. As a
default completeness guard, more than two unresolved images and more than 20%
of referenced images, any undefined active shader node, or a missing linked
library makes the source incomplete.

**Verify:** Preflight loads the scene in the target runtime, resolves active
dependencies, and distinguishes a dependency block from a GPU/runtime failure.

**Search terms:** `missing dependency`, `linked library`, `undefined shader`, `source completeness`

## Cap bright cool palettes without changing lights

**Applies when:** A palette edit moves white materials toward a cool hue in a
bright interior or studio scene.

**Rule:** Keep the palette value below clipping. Under the maintained palette
contract, output with more than 12% bright foreground and 8% near-white
foreground remains in review until material value is reduced. Do not alter
lights, geometry, or camera to hide the palette defect.

**Verify:** Foreground bright and near-white fractions pass after a material-only
rerender with frozen presentation.

**Search terms:** `cool white`, `palette clipping`, `near-white fraction`, `material-only repair`
