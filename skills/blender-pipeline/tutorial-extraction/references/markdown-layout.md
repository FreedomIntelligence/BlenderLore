# Human-readable Markdown tutorial layout

Use a restrained structure that renders well in common Markdown viewers and remains easy for an agent to parse.

- Begin with one specific H1 title, a short purpose statement, and the clearest final-result frame taken directly from the source video. Do not create a substitute model or render for the opening image.
- Near the start, include `You will need`, Blender 4.1 / 5.1.2 compatibility, exact learner inputs (or an explicit statement that `input/` is empty), and a compact parameter table when useful.
- Give each step an outcome-oriented numbered H2 heading with its timestamp. Follow it with concise actions, one relevant image, a plain caption, a visible completion signal, and a practical correction where needed.
- End with a structural recap, save advice, and the source URL. Do not expose grading or verifier details to the task-performing agent.
- Store all tutorial images in `output/image/`. Reference them with Markdown image syntax and POSIX-style relative targets from the tutorial, for example `image/frame_023.jpg` with alt text describing the bevel settings.
- Use descriptive alt text. Never use absolute filesystem paths, `file:` URIs, remote hotlinks, base64 data, `../`, or URL-escaped local paths.
- Keep screenshots large enough to identify the editor, panel, node, or control. Crop phone-video margins only when they add no evidence.
- Avoid ornamental badges, marketing copy, repeated metadata, raw HTML layout, and statements about the document format itself.

QA the rendered Markdown, not only its source. Require readable Chinese text, working relative image links, accurate captions, useful heading hierarchy, tables that remain compact, and no unreferenced image files. The first referenced image must byte-match the selected source-video frame.
