---
name: blender-tutorial-replay-supervision
description: Use this skill when turning Blender tutorial videos into tutorial-driven Blender assets or animations, especially when quality depends on staged visual supervision against reference frames.
---

# Blender Tutorial Replay Supervision

Use this workflow for Blender tutorial replay tasks where generated `asset.blend`, `render.png`, or `animation.mp4` must visually match a source tutorial.

## Required Artifact Chain

- Treat `tutorial.md`, source frames, `final_reference.png`, generated Python, Blender logs, `render.png`, and `animation.mp4` as one auditable chain.
- Do not accept success from file existence alone. Blender return code `0` only proves execution, not visual quality.
- Keep the last useful baseline for comparison; delete failed throwaway runs when they no longer help diagnosis.
- In a long `tutorial.md`, treat the step-by-step graphic tutorial section as the execution spine. API-compatibility text is only a boundary for code generation, not a replacement for the tutorial steps.
- Never overwrite a useful baseline run. Use a new output directory for every material experiment; pass overwrite flags only when intentionally replacing a known-bad run.
- Validate `final_reference.png` before using it as visual target. If OCR/title cues show a title card, course promo, lecturer slide, or website/video UI rather than the finished Blender asset, mark it auxiliary/invalid and do not let it constrain generated objects or composition.
- Title-level scene words such as `咖啡馆`, `咖啡店`, `室内`, `房间`, `场景`, `cafe`, or `interior` must route to scene/environment constraints before OCR-derived character/animal terms are considered.

## Staged Render Gates

Run generation in this order:

1. **Code gate**: compile generated Python and reject syntax errors before Blender.
2. **Completeness gate**: require real `save_as_mainfile`, `render.render`, executable entrypoint, and mp4 assembly logic; filenames in strings are not enough.
3. **Static gate**: render only `render.png` and `asset.blend`.
4. Inspect `render.png` directly against `final_reference.png`.
5. If static shape, material, camera, or silhouette fail, stop. Do not render animation.
6. **Preview gate**: render a low-frame `animation.mp4` only after static passes.
7. Inspect animation contact sheet for motion, drag, framing, and material stability.
8. **Full gate**: render full animation only after preview passes.

## Asset Presentation Layer

- Treat the final showcase video as a presentation layer, not an asset-generation layer. It may adjust camera movement, lighting, background color, studio floor, and framing, but it must not change tutorial-verified geometry, material facts, or animation behavior.
- Pick the showcase style from `motion_plan.json`:
  - `cinematic_scene`: composed scene/interior assets use a mostly stable camera, low/front perspective, darker cinematic world color, and subtle push/orbit.
  - `character_loop`: characters or rigged assets use a clean studio floor/background plus subtle bob/sway or tutorial-provided motion.
  - `detail_showcase`: transparent, material, shader, flower, glass, or lab-like assets use a restrained hold/slow push so surface detail remains readable.
  - `studio_turntable`: static props use a slow partial turntable or push-in, not a full fast spin.
- If the tutorial contains explicit motion, reproduce that motion first. If no explicit motion exists, use a polished static showcase instead of inventing unrelated action.
- Six-view renders remain diagnostic and asset-focused; `final_effect.mp4` is the polished presentation deliverable.

## Code-as-Room Lessons

- Do not let the LLM be the only judge after rendering. After each Blender gate, export scene diagnostics first: object count, curve count, curve point count, materials, camera, frame range, and keyframe data.
- Run deterministic visual metrics before LLM visual QA. For reference-like dark-background assets, check foreground coverage, bounding box fill, color-family ratio, neutral/gray exposure, and sampled animation motion.
- Treat deterministic failures as hard evidence in the repair prompt. The LLM may add interpretation, but it must not ignore numeric failures such as too-small object coverage, exposed gray base mesh, or too-few fur strands.
- Use the LLM for semantic exceptions and targeted repair only after structural diagnostics and image metrics have run.
- Preserve the full upstream chain at every stage: tutorial, image evidence, constraint manifest, previous output, and current diagnostics. Do not feed only the previous stage summary.

## SEIG Stage Isolation

- Use SEIG-style staged executable generation for `tutorial -> Blender`: scaffold, geometry, material, composition-camera, lighting-render, then physics-animation.
- Each stage must declare allowed edits and forbidden edits. A stage patch that touches forbidden fields fails even if the render looks better.
- Save an approved `.blend` checkpoint and diagnostics JSON after every stage. Later repairs start from the last approved checkpoint, not from a whole-script rewrite.
- Verifier output must be checklist JSON: approved, passed items, failed items, regressions, and next action. Avoid vague critique as the only repair signal.
- Physics/animation is its own final stage. When dynamics fail, patch only keyframes, force fields, particle/cloth/rigid settings, bake/cache, and preview frames; do not modify approved geometry, material, camera, or lighting.
- Mainline SEIG entry should write `seig_stage_manifest.json`, `seig_execution_plan.json`, `constraint_manifest.json`, `local_review_seig_static.json`, and `seig_generated.py` before any Blender render.
- Preserve fallback explicitly. A SEIG run may fail or underperform; keep `--generation-mode template|llm|spec` available and never overwrite the last good baseline while testing SEIG.
- SEIG is most useful when a warning points to a precise failed stage. For example, camera tracking that hides a frame-1-to-frame-20 displacement is a `composition_camera` issue, not a reason to rewrite geometry, material, or particle parameters.
- A SEIG wrapper alone does not improve the asset. If the generated script is only a copied seed script, the output will not change. Stage warnings must either produce an executable stage patch or be recorded as unresolved.
- Do not blindly render the hero `render.png` at a late post-simulation frame. If hair/cloth/fluid dynamics are unstable, late frames can turn a good static baseline into a visibly bad render. Keep `render.png` for the target/static gate and use preview/contact frames for dynamics validation unless the reference explicitly requires a late frame.
- Mark mixed outcomes explicitly, for example `static_pass_preview_fail`. File existence plus good first frame is not animation success.

## Review Split

- Build `constraint_manifest.json` from `tutorial.md`, image hashes, final reference, execution plan, output contract, and non-regression constraints before code generation.
- Local review is the hard gate. It checks API safety, required outputs, stage gates, manifest consistency, density/parameter constraints, scene diagnostics, and deterministic visual metrics.
- Gemini review is only a visual/semantic advisor. It may identify likely visual drift, but it cannot change locked parameters, reduce density, remove animation, remove outputs, or override `constraint_manifest.json`.
- Once `tutorial.md` or `tutorial_qgate.md` passes local constraint review, visual-only QA failure must not automatically trigger a Gemini script rewrite. Stop and apply a minimal local/single-variable repair, or require an explicit experiment flag before allowing model visual repair.
- Audit Gemini review output before using it. If it suggests weakening a hard constraint, reject that review and do not run the rewrite.
- Treat Gemini semantic rewrites as candidates only. If a rewrite fails local hard review, discard it and continue from the last locally clean script.
- After runtime/visual repair, prefer local hard review plus regression audit over another Gemini semantic rewrite; repeated semantic review can be slow and may reintroduce operation drift.
- Nonblocking warnings must not stop repair loops. Only `severity=error` should halt execution or discard a candidate.

## Operation Fidelity

- For tutorial replay, operation fidelity outranks visual similarity. The model must first implement the verified UI operations and exact mentioned parameters; visual QA cannot authorize changing them.
- Do not turn a particle-system tutorial into curve strands, mesh hair, procedural objects, or a category-level visual approximation unless the user explicitly approves that fallback after an API failure.
- Local review must check concrete code symbols for each verified operation, for example `PARTICLE_SYSTEM`, `settings.type = "HAIR"`, `psys.use_hair_dynamics = True`, `point_cache.frame_end`, frame 1/20 keyframes, `ptcache.bake_all`, `camera_add`, and `ShaderNodeBsdfHairPrincipled`.
- Unmentioned parameters stay at Blender defaults. Adding extra parameters for visual improvement is a pipeline failure unless the user has approved that specific change.

## Q-Gate Tutorial Extraction

- Convert every tutorial operation/parameter into a question before writing `tutorial.md`: "what was changed?", "what is the final stable value?", and "which post-change frame proves it?".
- Select evidence from a dense frame window around the cue, not only the frame where subtitles mention the parameter. The accepted frame should show the parameter after the cursor/edit has landed.
- Only verified operations enter the executable tutorial. If the value cannot be read from OCR, UI evidence, subtitle/ASR, or user correction, mark it `unverified`; never let the LLM guess.
- Bind each accepted operation to an `IMAGE_ID` and keep the image path table in `tutorial.md`. When sending images to a model, label every upload with the same `IMAGE_ID` used in the document.
- Do not let `tutorial.md` become image-heavy and text-light. Every key evidence image/window must be converted into explicit textual constraints: visible objects, materials/colors/textures, surface details, spatial layout, final constraints, and do-not-omit items.
- Gate tutorial quality with `tutorial_visual_text_review.json`. A tutorial with many images but no `视觉结果`, `材质/颜色/纹理`, `空间关系`, `表面细节`, or `tutorial_visual_contract.json` must fail before Blender code generation.
- User corrections override model/OCR output and become hard truth in `tutorial.md`, `constraint_manifest.json`, and local review. Keep the correction as a reusable operation contract, not as a one-off note.
- Q-Gate improves static quality by moving the source of truth from "LLM summarized the video" to "operation plus stable evidence frame plus local constraint review".
- For Blender tutorials, use step-segmented OCR-QGate rather than single-query single-frame Q-Gate. Each step should keep an evidence pack: step start, best overall, post-change stable, and OCR-best frame.
- Use scoring weights: time-window/context 0.20, visual change/clarity 0.20, UI grounding/readability 0.20, OCR parameter hit 0.40.
- OCR should read the Blender UI parameters, not only subtitles. UI values such as numeric fields, dropdown labels, modifier names, timeline frame numbers, and node names are stronger evidence than narration.
- OCR must be active in the manifest, not merely declared in the scoring formula. Default pass should use a lightweight UI-region OCR probe; if a step is low confidence or the parameter is briefly visible, rerun only that step/window with high-precision OCR instead of high-precision scanning the whole video.
- Match Blender UI OCR with bilingual aliases. For example, `Hair Length 0.3` may appear as `头发 0.3`, `Children Interpolated` may appear as `插值/差值`, and `Principled Hair BSDF` may appear as `原理化毛发`.
- Infer asset family from title and verified tutorial steps first. OCR-only words such as `cat`, `dog`, `face`, `eye`, `rig`, or `skin` must not reclassify a general prop, shader, liquid, or scene tutorial into a character/animal pipeline without title or step confirmation.
- Explicit object titles such as `圣诞树`, `树`, `植物`, `苹果`, `基础模型`, `christmas tree`, `tree`, `plant`, or similar concrete props should route to `general_asset` unless the title itself says character/animal/scene.
- Do not treat fabric/material words as simulation by default. `布料` or `cloth` only makes a dynamic task when paired with `模拟`, `动力`, `解算`, `膨胀`, `碰撞`, keyframes, or equivalent explicit motion terms.

## Anti-Overfit Rule

- Do not promote a pipeline rule because it works on one video. A tutorial-extraction rule must pass at least one other unrelated tutorial type before it becomes mainline.
- Keep asset-generation fixes separate from tutorial-extraction fixes. If Q-Gate improves `tutorial.md` but asset output fails, record the failure as a `tutorial -> Blender execution` issue, not a Q-Gate issue.
- For new Blender effect families, run a small validation set: one hair/particle video, one cloth/soft-body video, and one rigid/modeling or shader-node video when available.
- Mark video-specific constraints explicitly. A rule derived from `maoqiu` cannot silently become a general hair template unless another hair tutorial validates it.
- Do not inject hair/fur or maoqiu-specific prompt rules into non-hair tutorials. Route-specific rules must be conditional on the inferred family. For rigid character/modeling videos, fur parameters, hair dynamics, Brownian, clump, and particle-hair script structures are forbidden unless the tutorial explicitly uses them.
- Static-only videos must be able to stop at the static gate. The script may support `ASSET_RENDER_STAGE`, but `ASSET_RENDER_STAGE=static` should require only `asset.blend` and `render.png`; do not force `animation.mp4` for a static modeling tutorial.
- For character/prop white-model tutorials, first check camera-side consistency: if accessories are placed on `+Y`, the camera must face that side. Prefer a `look_at` helper using `mathutils.Vector(...).to_track_quat('-Z','Y')` over guessed Euler rotations.
- LLM semantic review warnings are useful as a local repair list, not a reason to restart the whole script. For the gentleman-penguin case, warnings correctly identified rounded-rectangle glasses, integrated toes, and sharper arm bends; local deterministic repair improved the render faster than a full model rewrite.
- For static character props, fix category-critical visible geometry locally when possible: triangular beak as a wedge mesh, glasses as rounded-rectangle bevel curves, curve limbs with `use_fill_caps=True`, orthographic front camera, and bright clay material/viewport-like lighting.

## Batch Replay Anti-Collapse

- A fast path such as `video/title/contact_sheet -> fast_asset_spec.json -> generic procedural template` is smoke-test only. It must not be counted as production replay, even if `asset.blend`, `render.png`, and `animation.mp4` exist.
- For batch production, require the same source-of-truth chain as a single supervised video: `tutorial.md`, evidence images, `steps_verified.json`, `execution_plan.json`, `constraint_manifest.json`, local review reports, and staged render gates.
- Reject output directories that contain `fast_asset_spec.json` when deciding whether a video is complete, unless the user explicitly asks for a smoke test.
- Category-level asset families such as hydrogel, graphene, fiber, nanotube, molecule, building, product, or food are not enough. The tutorial must lock video-specific subject structure, object hierarchy, distinctive visible parts, material cues, and final-reference constraints.
- Similar scientific tutorial titles tend to collapse into balls, curves, particles, and translucent shaders. Prevent this by extracting per-video operations and evidence before asking a model for Blender code.
- Do not solve batch throughput by skipping tutorial QA, execution planning, script semantic review, deterministic visual audit, or visual QA. If time is not constrained, slow down and preserve constraints.
- If many batch outputs look alike while file hashes differ, treat it as template/constraint collapse, not a file-copy bug. Inspect `fast_asset_spec.json`, generated code family, and whether the strong pipeline was bypassed.
- The first batch after a pipeline change should be a small quality batch, not 30-60 videos. Run 2-3 different tutorial families and manually inspect `tutorial.md`, `constraint_manifest.json`, `render.png`, and `animation.mp4` before scaling.

## Dynamic Effect Gates

- For dynamics tutorials, `render.png` should represent the tutorial target frame or a post-simulation frame, not automatically frame 1. Frame 1 can be a setup pose with no gravity, cloth, fluid, or hair response yet.
- Preview animation must include both the keyframed impulse window and post-impulse relaxation frames. For a frame 1 to frame 20 motion, sample frames after 20, such as 40/60/90/120/150, to verify inertia and gravity.
- Do not blindly choose very late post-impulse frames as the hero render. First inspect a short post-motion window; if late frames collapse the simulation into curtains or spikes, use earlier verified target frames and record the late-frame failure.
- A camera that tracks the moving object can hide translation and make the animation look wrong or static. Frame the whole motion envelope with a fixed camera or a non-animated midpoint target unless the tutorial explicitly tracks the subject.
- For hair dynamics, verify three separate facts: `psys.use_hair_dynamics = True`, the cache is baked after both keyframes, and the rendered preview shows strand-tip lag or gravity response after the motion.
- For the fur-ball tutorial, Step 6 material setup happens after playback/bake and camera setup. The material Surface must be `ShaderNodeBsdfHairPrincipled` linked to `Material Output` `Surface`; checking the node alone is not enough if execution order is wrong.
- Audit operation order from the `main()` execution calls, not raw text positions. A function definition containing `ShaderNodeBsdfHairPrincipled` may appear before `bake_dynamics()` in the file while still being called after bake.
- If the tutorial clicks timeline Play after setting keyframes, map it explicitly. In scripts, return to frame 1 and evaluate/play the relevant frame range before baking; otherwise the output may become a few static sampled states rather than a continuous dynamics preview.
- When a particle-hair system is created after a subdivision modifier, set `settings.use_modifier_stack = True`. Without this, hair may emit from the unsubdivided base mesh and dynamic frames can expose smooth surface patches even when child/display counts are correct.
- Successful fur-ball pipeline checkpoint: use Q-Gate/user-corrected `tutorial_qgate.md`, preserve hard particle parameters, create particle hair after subdivision with `use_modifier_stack=True`, keyframe frame 1 upward and frame 20 original position, evaluate/play frames 1-20 before `ptcache.bake_all`, then camera, then Principled Hair BSDF material. Render preview as continuous frames, not sparse samples.
- Smooth animation preview should render enough unique frames and encode at the intended fps. A 30-frame preview at 8fps is acceptable for quick checks; for presentation, render 90+ continuous frames at 24fps rather than only changing ffmpeg fps on the same 30 frames.
- To make the whole fur object move, keyframe the source sphere object itself, not the particle strands. Keep particle roots attached through the native particle system, evaluate/play the full keyframed range before baking, widen the camera to show the motion envelope, and render continuous frames. Otherwise the hair may move while the object appears static because the camera crop hides the source movement.
- When a user specifies timed animation phases, convert the words into an explicit frame table before rendering. At 120fps, "first 20 frames", "next 3 seconds", and "then 3 seconds" must become fixed frame ranges. Do not let long-distance interpolation span a pause; insert same-transform hold keyframes, then short-interval fast-move keyframes.
- Before any expensive full animation render, audit the generated keyframe velocity table: frame A -> frame B, seconds, distance, speed, and phase label. If a supposedly fast segment contains low-speed long drifts, fix keyframes before rendering.
- For visible side-to-side object swing, choose the rotation axis relative to the camera. A Y-axis tilt can look subtle or hidden when the camera views along Y; use a stronger visible roll/yaw axis when the requested effect is left/right wobble.
- For 4K/high-fps dense hair or fur, low-bitrate inter-frame H.264 can create yellow/macroblock artifacts during fast motion. Use PNG frame renders plus high-quality encoding such as all-intra H.264 (`-crf 10 -g 1 -bf 0`) or a visually equivalent high-bitrate codec, and disable motion blur unless the tutorial asks for blur.
- Remote pullback must not treat `test -s animation.mp4` as completion. Wait for Blender/ffmpeg process exit or stable file size, then verify with `ffprobe` before copying to local. Otherwise the monitor can copy a partially written mp4 and leave a bad local result.
- For particle/cloth/fluid bake steps, require visible progress markers before and after bake. A script that silently spends minutes before creating `render.png` is not a reliable pipeline run; classify it as a bake observability/timeout problem before changing visual parameters.
- Qualitative motion words such as "move up a little" are constraints, not free variables. Estimate the displacement from the video; if unverified, bound it conservatively relative to object size. For a unit-radius hair ball, a "small upward move" should be far below one full radius unless evidence proves otherwise.
- If a dynamics preview turns a ball into a curtain, spike tower, or detached sheet, first suspect excessive keyframed displacement or camera/target framing before changing tutorial parameters.
- For a good static baseline with weak dynamics, branch from that exact baseline and alter only one dynamics inspection variable at a time, such as bake/render/save frame selection or preview frame sampling. Do not ask the model to regenerate the whole script.
- For the fur-ball tutorial, moving a unit-radius sphere from `z=1.5` back to `z=0` is too large for the source wording "move up a little" and can make later dynamic frames lose visible fur. A smaller upward offset such as `z=0.35` preserves the fur shape better; treat this as an animation-amplitude correction, not a particle-parameter change.

## Pipeline Hardening Order

When the pipeline fails repeatedly, fix in this order:

1. **Tutorial priority drift**: keep Section 5 as the execution spine and inject local non-regression constraints into any LLM execution plan.
2. **One-pass over-rendering**: require `ASSET_RENDER_STAGE=static|preview|full`; stop at static if shape/material/camera fails.
3. **Weak hard constraints**: do not trust an LLM plan that omits `non_regression_constraints`; merge transparent local constraints before code generation.
4. **Late visual failure**: run visual QA at the static and preview gates, not only after full animation.
5. **LLM-only visual QA**: add deterministic scene/image diagnostics before model review, and fail early on measurable mismatches.
6. **Repair regression**: visual repair must not reduce density, remove stage gates, remove outputs, or weaken verified parameters.
7. **Model/network instability**: retry request exceptions and record exact failed stage; do not silently continue with missing model outputs.
8. **Visual QA subjectivity**: pair LLM visual QA with deterministic metrics and direct artifact inspection; final pass still requires viewing render/video against `final_reference.png`.
9. **Stale artifact pollution**: clear `asset.blend`, `render.png`, `animation.mp4`, `frame_*.png`, `anim_*.png`, and old contact sheets before every gate.

## Visual QA Checklist

Check concrete visual dimensions:

- shape and silhouette
- camera framing and cropping
- material colors and depth
- object-specific structure
- hair/cloth/fluid/rigid behavior
- frame-to-frame motion consistency
- whether the result is merely category-correct or truly tutorial-correct

For fur/hair tutorials:

- Fail straight radial needles, straw-like grass, exposed smooth base, uniform tan/brown color, and rigid whole-object motion.
- Require dense attached roots, soft curved strands, visible clumping, dark red depth, light orange tips, and tip lag during motion.
- For visual repair, keep tutorial particle parameters fixed and adjust only allowed visual controls: camera framing, look-at target, background/world color, lighting, render engine/display mode, and the allowed orange-red color.
- When deterministic metrics report poor framing or empty foreground, require a camera look-at helper (`mathutils.Vector(...).to_track_quat('-Z','Y')`) rather than guessed Euler angles.
- When red/orange is too low, avoid dark brown values; when luma is too high, add dark-red depth/shadow while preserving orange highlights.
- Treat curve-only, particle-only, and hair-dynamics attempts as separate branches. If a branch produces brush heads, cotton balls, pasted ribbons, or hair curtains, stop at the static gate and roll the template back before continuing.
- Use runtime API checks before relying on specialized hair features. `ShaderNodeHairInfo` can help root-to-tip color, but it does not solve shape or dynamics by itself.
- Do not promote patch-emitter, particle-only, or ultra-thin-curve branches unless `render.png` passes the static gate; execution success is not evidence of reusable template quality.
- For moving assets, frame the full motion envelope, not just the first frame. A centered first frame can still fail if later frames leave the camera.
- When assembling mp4 from sampled frames, write sequential image names such as `anim_0001.png`; sparse source frame numbers can break `%04d` ffmpeg inputs.

## Supervision Discipline

- Watch the run at every gate; do not wait for a full expensive render when the static result is already wrong.
- Record the specific failed layer: tutorial extraction, template mapping, Blender API/runtime, material, camera, static geometry, or animation behavior.
- If a motion pass destroys a good static result, reset to the last good static branch instead of patching the degraded branch.
- Avoid extra README or explanatory files. Only keep scripts, logs, JSON summaries, images, videos, and assets that support the task.
- Configure remote GPU hosts through environment variables or private config files; do not commit passwords, cookies, or API keys. When rendering/generation workers finish or are stopped, leave a `tmux` session running the configured GPU hold script if the deployment requires it. If `tmux` is missing or the hold script cannot run, explicitly report that the hold requirement could not be satisfied.
