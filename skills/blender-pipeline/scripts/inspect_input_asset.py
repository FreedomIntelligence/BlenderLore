"""Trusted Blender-side, non-saving inspection of a staged source asset."""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import bpy


def image_dependency_paths(item, path: Path) -> list[Path]:
    """Resolve declared UDIM tiles, rather than looking for a literal token."""
    if item.source != "TILED":
        return [path]
    tiles = list(item.tiles)
    if not tiles:
        return [path]
    value = str(path)
    result = []
    for tile in tiles:
        number = int(tile.number)
        if "<UDIM>" in value:
            target = value.replace("<UDIM>", str(number))
        elif "<UVTILE>" in value:
            target = value.replace(
                "<UVTILE>", f"u{(number - 1001) % 10 + 1}_v{(number - 1001) // 10 + 1}"
            )
        else:
            # Blender may retain the first concrete tile name instead of a
            # token. Replace only a standalone conventional four-digit tile ID.
            target, count = re.subn(
                r"(?<!\d)1\d{3}(?!\d)(?=[^/\\]*$)", str(number), value, count=1
            )
            if not count and len(tiles) > 1:
                return []
        result.append(Path(target).resolve())
    return result


def inspect(asset: Path, root: Path) -> dict:
    if asset.suffix.lower() != ".blend":
        # Interchange files do not replace the current scene. Factory startup
        # contains a cube, camera and lamp that must not become source assets.
        bpy.ops.wm.read_factory_settings(use_empty=True)
    if asset.suffix.lower() == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(asset), load_ui=False, use_scripts=False)
    elif asset.suffix.lower() == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(asset))
    elif asset.suffix.lower() in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(asset))
    elif asset.suffix.lower() == ".obj":
        bpy.ops.wm.obj_import(filepath=str(asset))
    issues = []
    images = []
    for item in bpy.data.images:
        if item.source not in {"FILE", "TILED", "SEQUENCE", "MOVIE"}:
            continue
        packed = bool(item.packed_file or item.packed_files)
        path = Path(bpy.path.abspath(item.filepath, library=item.library)).resolve()
        dependencies = image_dependency_paths(item, path) if not packed else []
        if not packed and (
            not dependencies
            or any(not p.is_file() or not p.is_relative_to(root) for p in dependencies)
        ):
            issues.append(
                f"Image dependency is missing or outside the staged bundle: {item.name}"
            )
        images.append(
            {
                "name": item.name,
                "packed": packed,
                "path": str(path) if not packed else None,
                "tile_paths": [str(p) for p in dependencies]
                if item.source == "TILED"
                else [],
            }
        )
    for lib in bpy.data.libraries:
        path = Path(bpy.path.abspath(lib.filepath)).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            issues.append(
                f"Linked library is missing or outside the staged bundle: {lib.name}"
            )
    for material in bpy.data.materials:
        if material.use_nodes and material.node_tree:
            if any(n.bl_idname == "NodeUndefined" for n in material.node_tree.nodes):
                issues.append(f"Unsupported shader node in material: {material.name}")
    return {
        "schema": "blender-pipeline-input-audit.v1",
        "selected_model_sha256": hashlib.file_digest(
            asset.open("rb"), "sha256"
        ).hexdigest()
        if hasattr(hashlib, "file_digest")
        else hashlib.sha256(asset.read_bytes()).hexdigest(),
        "status": "blocked" if issues else "pass",
        "issues": issues,
        "blender_version": bpy.app.version_string,
        "objects": [
            {
                "name": o.name,
                "type": o.type,
                "vertices": len(o.data.vertices) if o.type == "MESH" else None,
                "materials": [m.name if m else None for m in o.data.materials]
                if hasattr(o.data, "materials")
                else [],
                "modifiers": [m.type for m in o.modifiers],
            }
            for o in bpy.context.scene.objects
        ],
        "images": images,
        "frame_range": [bpy.context.scene.frame_start, bpy.context.scene.frame_end],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])
    result = inspect(args.asset.resolve(), args.root.resolve())
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if result["issues"]:
        raise RuntimeError("; ".join(result["issues"]))
