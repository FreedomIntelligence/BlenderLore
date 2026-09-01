---
name: blender-pipeline-reproduction
description: Reproduce a maintained Blender showcase recipe from authorized video, tutorial, image, or local asset inputs while enforcing visual-acceptance and asset-distribution gates.
---

# Blender Showcase Reproduction

Use `scripts/reproduce.sh` to select one exact recipe. The command is a dry run
unless the user explicitly authorizes `--execute`.

The packaged public catalog is metadata-only. It records the current public
showcase identity, input bindings, dynamic acceptance contracts, known gaps,
and distribution policy; it contains no source assets or tutorial media. A
public preview URL is acceptance evidence, not permission to download or reuse
the asset. A recipe that is disabled, blocked, incomplete, or not independently
accepted must fail before download, model invocation, or Blender execution.

## Tutorial inputs

- For an authorized existing tutorial, use `--tutorial-mode provided` and one
  or more `--tutorial` files. The operational truth is `tutorial.md` plus
  `steps_verified.json`.
- To extract an operational tutorial from video, explicitly use
  `--tutorial-mode extract`, `--video-url`, at least one `--image`, and a
  positive `--tutorial-window-budget`. Extraction uses the generation
  pipeline's rich evidence route and defaults to `gpt-5.6-sol`; `gpt-5.5` is
  an explicit fallback only.
- Paid extraction requires `BLENDER_PIPELINE_API_ENDPOINT` and an owner-only
  secret file named by `BLENDER_PIPELINE_API_KEY_FILE`. Dry runs do not read
  credentials, download media, call a model, or write a run directory.
- Add `--render-tutorial-html` only when a human-facing display copy is useful.
  It is rendered from the same verified steps without a second model call and
  never replaces `tutorial.md`.

## Asset delivery

Follow the selected recipe's `distribution_contract.asset_delivery` exactly:

- `bundled-open`: allowed only with package-local source, exact hash, SPDX
  identifier, and package-local license evidence.
- `user-supplied`: require a lawful local `--asset`; never fetch, publish, or
  commit it or the isolated staged copy.
- `blocked`: stop. An adapter, public URL, or matching-looking asset does not
  unlock the recipe.

Read [references/execution-contract.md](references/execution-contract.md) when
adding a recipe or diagnosing a validation failure.
