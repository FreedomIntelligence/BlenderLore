# Codex conversation launcher

## Interpret the request

The interaction is: select `$blender-pipeline`, then provide the video/tutorial
and any supporting files in normal language. Codex constructs and runs the
command. Do not ask the user to translate their request into CLI arguments.
If the skill is selected without any task input, ask for the video URL/file or
Markdown tutorial. Honor requests to explain or preview without execution.

Resolve the intended output before starting downloads, model calls or Blender:

| Input | Tutorial-only workflow | Reconstruction workflow |
| --- | --- | --- |
| Video URL or local video | Extract illustrated Markdown; optional HTML | Extract → retrieve knowledge → reconstruct → render |
| Video with starter project / supporting files | Extract with the supplied learner inputs | Preserve the starter and use its dependencies during reconstruction |
| Markdown tutorial, optionally with assets | Prepare existing Markdown/images; optional HTML, no video extraction | Skip extraction → retrieve knowledge → reconstruct → render |

If a video is supplied without an explicit goal, ask: “你希望只生成图文教程，还是继续复现视频中的 Blender 作品？”
For Markdown, ask: “你希望整理这份图文教程，还是按教程生成 Blender 作品？”
Use the user's language. A clear request such as “只要教程”, “转 HTML”, or
“复现并生成 .blend” already answers this question; do not ask again or treat
the Skill's generic selection prompt as authorization for reconstruction.
Wait for the answer; combine genuinely missing source/output questions when
possible. Follow the user's latest choice, not a default inferred from input type.

Accept multiple independent video links in one message. Run them sequentially,
with one `--video-url` per helper invocation and a separate subject/BVID-named
child directory under the selected output root. Ask about the goal once for
the batch, not once per video; preserve any explicitly different per-video goals
and supporting inputs. Do not pass a list or repeat `--video-url` in one call.
If the user intends several videos to contribute to a single asset, clarify
that separate runs do not merge tutorials instead of silently discarding links.

Select one primary source and preserve each attachment's actual role:

| Supplied input | Internal argument |
| --- | --- |
| Video link | `--video-url` |
| Local video file | `--video-file` |
| Existing Markdown tutorial | `--tutorial` |
| Starting `.blend`, `.glb`, `.gltf`, `.obj`, or `.fbx` | `--asset` |
| Complete dependency directory containing that model | `--asset-root` |
| Texture or other learner resource | repeat `--input-asset` |
| Starting-asset preview | repeat `--preview` |
| Finished-result reference | `--target-image` |
| Timestamped transcript | `--transcript` |

A video plus a starter project stays within the selected workflow, not a separate edit.
Markdown may also include a starter project or textures. Retain the tutorial's
relative images and the model's dependency structure. Do not use a starting
preview as a texture or final target. If an image's role or the primary source
is genuinely ambiguous, ask one concise question before launching. A text-only
Markdown replay needs a finished-result image; ask for it when absent. Do not
silently convert a full replay into extraction-only to avoid missing input.

## Resolve the run location

Use the output directory explicitly requested by the user. Otherwise select a
new subject-named run directory beneath the user's established data root from
the conversation or `BLENDER_PIPELINE_OUTPUT_ROOT`. Add a timestamp when needed
to avoid overwriting an earlier run. If no data location is established, ask
once where to save the result. Respect external-disk requirements; never
silently fall back to the system disk if that location is unavailable.

Resolve the current skill directory from this `SKILL.md` location, following
symlinks, rather than assuming the shell's working directory is the repository.
Resolve supplied relative file paths against the user's workspace before
invoking the helper. Quote each path as one argument, including spaces and
non-ASCII names. Do not write inputs or results into the installed skill/repo.

## Launch and finish

1. After resolving the goal, briefly state the selected workflow, mapped inputs
   and output location, then execute without another confirmation round.
2. Invoke `scripts/launch_from_codex.py` under the resolved skill directory with
   the selected source, output directory and supporting arguments. For example,
   the following are internal invocations performed by Codex, not instructions
   to hand back to the user:

   ```text
   python3 "<skill-dir>/scripts/launch_from_codex.py" --video-url "<url>" --extract-only --render-html --output-dir "<new-run-dir>"
   python3 "<skill-dir>/scripts/launch_from_codex.py" --video-url "<url>" --output-dir "<new-run-dir>"
   python3 "<skill-dir>/scripts/launch_from_codex.py" --video-file "<video>" --asset "<starter.blend>" --preview "<before.png>" --output-dir "<new-run-dir>"
   python3 "<skill-dir>/scripts/launch_from_codex.py" --tutorial "<tutorial.md>" --output-dir "<new-run-dir>"
   ```

   Prefer the repository's `.venv/bin/python` (`.venv/Scripts/python.exe` on
   Windows) created during setup; otherwise use a compatible configured Python.
   The helper uses the signed-in Codex CLI for model calls. Do not ask for an
   API key or switch to API mode. Forward a user-supplied Blender executable as `--blender`.
   Missing Blender, FFmpeg, Python dependencies, or Codex login should be
   reported specifically; do not install large packages or launch a different
   workflow without user direction. Retain `gpt-5.6-sol` and the existing bounded
   call/repair settings; use `gpt-5.5` only through the supported explicit fallback.
3. Map tutorial-only to `--extract-only`, and reconstruction to no such flag.
   `--check` and `--dry-run` are only for an explicit request for those operations.
   Use `visual` extraction by default (recommended for videos up to ten minutes);
   an explicit original rich/base64 request maps to `--tutorial-method legacy-rich`.
   Add `--render-html` only when requested. For an explicit rendering choice,
   set `VIDEO2BLENDER_CYCLES_BACKEND` for this invocation: `CPU`, `METAL` (Apple),
   `OPTIX`/`CUDA` (NVIDIA), `HIP` (AMD), or `ONEAPI` (Intel), as supported locally.
   Otherwise preserve the configured backend (default CPU); do not guess a GPU.
4. Let the helper manage extraction, knowledge lookup, scene generation and
   review. Do not run those stages a second time manually. Follow a running
   process until it finishes or needs input; do not return only its process ID.
   Do not automatically restart an entire paid run after an unclear failure.
5. For tutorial-only, read the exit status and link the produced `tutorial.md`,
   its local images, and requested HTML. Do not require `pipeline_review.json`
   or claim a Blender result. For reconstruction, read the exit status and
   `pipeline_review.json`; link the `.blend`, render, tutorial and any animation.
   Report a stopped stage or a result needing review as such, not full completion.

API use remains available separately through the unchanged API launcher. Do not
commit, push, publish results, or promote knowledge as a side effect of a Skill
launch unless the user separately requests it.
