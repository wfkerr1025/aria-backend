"""ARIA Lite - what this Blender can actually do, asked of Blender itself.

    python -m backend.blender.blender_api search extrude
    python -m backend.blender.blender_api show mesh.extrude_region_move
    python -m backend.blender.blender_api show types.Object
    python -m backend.blender.blender_api build          # re-ask Blender

WHY
---
run_python is only as good as the names written in it, and bpy names
move between versions -- this layer has already been caught out by
export_scene.obj (superseded by wm.obj_export), action.fcurves (empty
under 4.4's slotted actions) and a static engine list that named only
EEVEE. Memory of "the bpy API" is memory of several of them.

So the catalogue is not written: it is dumped from the installed
Blender -- every operator with its description and every property with
its type and default, and every bpy.types struct with its properties --
and cached per Blender version in the Blender output folder. Searching
it is instant; building it is one Blender launch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.blender import blender_actions

__all__ = ["build", "load", "search", "show", "main"]

# Runs inside Blender. Plain Python, not a template: it reads, it does
# not change the scene, and it takes no input.
_DUMP = r'''
import bpy, json, sys

def _prop(p):
    entry = {"name": p.identifier, "type": p.type, "description": p.description}
    if p.type == "ENUM":
        entry["items"] = [i.identifier for i in p.enum_items][:60]
    try:
        default = getattr(p, "default_array", None) if getattr(p, "is_array", False) else getattr(p, "default", None)
        if default is not None:
            entry["default"] = list(default) if hasattr(default, "__len__") and not isinstance(default, str) else default
    except Exception:
        pass
    return entry

ops = {}
for category in dir(bpy.ops):
    if category.startswith("_"):
        continue
    group = getattr(bpy.ops, category)
    for name in dir(group):
        if name.startswith("_"):
            continue
        try:
            rna = getattr(group, name).get_rna_type()
        except Exception:
            continue
        ops[category + "." + name] = {
            "description": rna.description,
            "properties": [_prop(p) for p in rna.properties if p.identifier != "rna_type"],
        }

types = {}
for name in dir(bpy.types):
    cls = getattr(bpy.types, name)
    rna = getattr(cls, "bl_rna", None)
    if rna is None or name.startswith("_"):
        continue
    types[name] = {
        "description": rna.description,
        "base": rna.base.identifier if rna.base else None,
        "properties": [{"name": p.identifier, "type": p.type, "description": p.description}
                       for p in rna.properties if p.identifier != "rna_type"],
    }

print("ARIA_API_OPEN")
print(json.dumps({"version": bpy.app.version_string, "ops": ops, "types": types}))
print("ARIA_API_CLOSE")
'''


def _cache_dir() -> Path:
    folder = blender_actions.output_dir() / "API"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _cached() -> Optional[Path]:
    found = sorted(_cache_dir().glob("blender_api_*.json"))
    return found[-1] if found else None


def build() -> Path:
    """Ask the installed Blender for its whole API and cache it. One launch."""
    import subprocess
    import tempfile

    executable = blender_actions.blender_path()
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as file:
        file.write(_DUMP)
        script = file.name
    try:
        run = subprocess.run([str(executable), "--background", "--factory-startup",
                              "--python", script], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=600)
    finally:
        Path(script).unlink(missing_ok=True)
    if "ARIA_API_OPEN" not in run.stdout:
        raise RuntimeError("Blender printed no catalogue:\n" + (run.stdout[-1500:] + run.stderr[-1500:]))
    data = json.loads(run.stdout.split("ARIA_API_OPEN")[1].split("ARIA_API_CLOSE")[0])
    path = _cache_dir() / f"blender_api_{re.sub(r'[^0-9.]', '', data['version'])}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def load() -> dict:
    """The cached catalogue, building it first if there is none."""
    path = _cached() or build()
    return json.loads(path.read_text(encoding="utf-8"))


def search(query: str, *, kind: str = "all", limit: int = 20) -> List[Dict[str, Any]]:
    """Operators and types whose name or description matches every word."""
    data = load()
    words = [w for w in re.split(r"\s+", query.lower()) if w]
    hits = []
    pools = ([("op", data["ops"])] if kind in ("all", "ops") else []) + \
            ([("type", data["types"])] if kind in ("all", "types") else [])
    for label, pool in pools:
        for name, entry in pool.items():
            haystack = (name + " " + (entry.get("description") or "")).lower()
            if all(w in haystack for w in words):
                # Name matches first, then shorter names: "extrude" should find
                # mesh.extrude_region before a type that mentions extruding.
                score = sum(3 for w in words if w in name.lower()) - len(name) / 100.0
                hits.append({"kind": label, "name": name, "score": score,
                             "description": entry.get("description") or ""})
    return sorted(hits, key=lambda h: -h["score"])[:limit]


def show(name: str) -> Optional[Dict[str, Any]]:
    """One operator ("mesh.extrude_region") or type ("types.Object" / "Object") in full."""
    data = load()
    if name in data["ops"]:
        return {"kind": "op", "name": name, **data["ops"][name]}
    key = name.split(".", 1)[1] if name.startswith("types.") else name
    if key in data["types"]:
        return {"kind": "type", "name": key, **data["types"][key]}
    return None


def _describe(entry: Dict[str, Any]) -> str:
    call = f"bpy.ops.{entry['name']}(...)" if entry["kind"] == "op" else f"bpy.types.{entry['name']}"
    lines = [call, entry.get("description") or ""]
    for p in entry.get("properties") or []:
        extra = ""
        if "default" in p:
            extra += f" = {p['default']!r}"
        if p.get("items"):
            extra += f"  one of {', '.join(p['items'][:12])}{'...' if len(p['items']) > 12 else ''}"
        lines.append(f"  {p['name']}: {p['type']}{extra}  -- {p.get('description', '')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.blender.blender_api",
                                     description="Search the installed Blender's own API.")
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("search")
    find.add_argument("query", nargs="+")
    find.add_argument("--kind", choices=["all", "ops", "types"], default="all")
    find.add_argument("--limit", type=int, default=20)
    one = sub.add_parser("show")
    one.add_argument("name")
    sub.add_parser("build")
    args = parser.parse_args(argv)

    if args.command == "build":
        path = build()
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"Blender {data['version']}: {len(data['ops'])} operators, "
              f"{len(data['types'])} types -> {path}")
        return 0
    if args.command == "search":
        for hit in search(" ".join(args.query), kind=args.kind, limit=args.limit):
            print(f"{hit['kind']:<5} {hit['name']:<45} {hit['description'][:80]}")
        return 0
    entry = show(args.name)
    if entry is None:
        print(f"Nothing called {args.name!r}. Try: search {args.name.split('.')[-1]}")
        return 1
    print(_describe(entry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
