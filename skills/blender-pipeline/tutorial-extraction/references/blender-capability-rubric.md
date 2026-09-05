# Blender capability rubric

Use exactly these labels:

| Label | Observable scope |
|---|---|
| `GEO` | Geometry, topology, hard-surface/organic modeling, curves, sculpting |
| `PROC` | Modifiers, instances, Geometry Nodes, procedural relationships |
| `SURF` | Materials, UVs, maps, procedural textures, displacement, transparency, volume |
| `SCN` | Scene assembly, lights, cameras, World, rendering, compositing |
| `RIG` | Armatures, skinning, constraints, Shape Keys, drivers |
| `ANM` | Keyframes, F-Curves, NLA, procedural and camera animation |
| `SIM` | Rigid body, cloth, fluid, soft body, particles, caches |
| `PIPE` | Editable structure, dependency closure, import/export, reproducible scripts |

## Score construction

1. Identify only the capabilities the video actually teaches.
2. Give each scored row a unique ID such as `SURF-03` and a positive point value.
3. Make the detailed rows sum to exactly 100. Category subtotals must equal their rows.
4. Define observable full, partial, and fail conditions. Avoid aesthetic judgments in scored rows unless they can be measured deterministically.
5. Implement the same IDs in an artifact verifier. Every scored ID must occur exactly once in its rubric table and be executable by the verifier.
6. Omit unneeded labels entirely; do not create placeholder points or unverifiable “overall quality” scores.

Emit the rubric itself as UTF-8 JSON. Include `schema_version`, title, source ID, artifact type, target Blender versions, `total_points`, `status_weights`, category totals, and scored rows. Each row contains its ID, capability, criterion, points, scoring rule, and the same artifact-verifier check ID.

Embed the complete Blender Python verifier under `artifact_verifier.python_source`; do not create a separate verifier file in `output/`. The source must contain every scored check ID and return the artifact path, Blender version, `points_earned`, `points_possible`, each check's ID, capability, status, awarded points, observed value, and expected condition.

The grading document is rubric-only. Keep subjective presentation notes outside it.
