"""ARIA Lite - getting a Blender export into Unity.

Blender writes an FBX somewhere temporary. Unity wants it under
Assets/, imported, made into a prefab, and dropped in a scene.

Those steps are not Blender's. Ludo.ai downloads a GLB and needs
exactly the same four, so they live in backend.unity.unity_delivery
and this module is the Blender-facing name for them. The measured
Pipeline argument names -- import_asset(source, path, confirm),
create_prefab(source, path), instantiate_prefab(prefab, scene_path,
name) -- are written down once, there, because two copies of a
measured fact drift and the one nobody updated is the one somebody is
using.

Kept as a module rather than folded away because `blender_asset_
pipeline.deliver_to_unity(...)` is the sentence a reader of the
Blender layer expects to find, and because the tests that pin this
behaviour were written against it.
"""

from __future__ import annotations

from backend.unity.unity_delivery import (  # noqa: F401 - re-exported
    DEFAULT_FOLDER,
    DEFAULT_PREFAB_FOLDER,
    MODEL_SUFFIXES,
    _failure,
    _object_ref,
    _run,
    asset_path_for,
    create_prefab,
    deliver_to_unity,
    move_export_to_unity,
    place_in_scene,
    trigger_unity_import,
    unity_project_root,
)

__all__ = [
    "asset_path_for",
    "create_prefab",
    "deliver_to_unity",
    "move_export_to_unity",
    "place_in_scene",
    "trigger_unity_import",
    "unity_project_root",
]
