# Blender action layer

Drives Blender 5.0.1 without letting a language model write the Python.

## Why it is shaped like this

Typed into ARIA chat, measured on this machine:

```
blender --background --python <script>
```

The turn reached phi-3-mini, which answered with:

```python
def main:
    bpy.ops.preferences.addonSettings(module="Blender3D", reset=True)
```

`def main:` is not valid Python and that operator does not exist. A model asked
to drive a program it cannot run does not decline — it writes what such a
request usually produces.

So the Python is generated from templates. A caller — a person, the natural
language mapper, or a model — chooses **which action** and **what numbers**, and
never a statement. Leaf values go through `repr()` (text), `float()`/`int()`
(numbers) or an allowlist (choices), so the worst a hostile object name can do
is be a strange object name.

## The four modules

| Module | Job |
| --- | --- |
| `blender_script_templates.py` | 39 actions → bpy source. The only place Python is written. |
| `blender_actions.py` | One script per run, temp file, shared CLI runner, parse the result. |
| `blender_nl_mapping.py` | A sentence → a list of actions, or `None`. |
| `blender_asset_pipeline.py` | An export → into Unity, imported, placed, prefabbed. |

### Actions available

Modelling (`add_cube`, `add_sphere`, `add_cylinder`, `add_plane`, `add_torus`),
modifiers (`apply_subdivision`, `apply_bevel`, `apply_mirror`, `apply_array`,
`apply_boolean`, `apply_solidify`, `apply_decimate`, `apply_multires`),
transforms (`move`, `rotate`, `scale`, `parent`), UV (`smart_uv_project`,
`mark_seams`, `unwrap`), materials (`create_material`, `assign_material`),
rigging (`create_armature`, `add_bone`, `parent_mesh_to_armature`,
`auto_weights`), skinning (`normalize_weights`, `assign_vertex_group`),
animation (`insert_keyframe`, `set_pose`, `set_frame`, `bake_animation`),
sculpting (`sculpt_brush`, `enable_dyntopo`), export (`export_fbx`,
`export_glb`, `export_obj`) and scene (`clear_scene`, `save_file`).

## Using it

```python
from backend.blender import blender_actions as actions

result = actions.run_actions([
    {"action": "clear_scene"},
    {"action": "add_cube", "params": {"name": "Chassis", "size": 2}},
    {"action": "apply_bevel", "params": {"object": "Chassis", "amount": 0.05}},
    {"action": "export_glb", "params": {"path": "D:/out/car.glb"}},
])
```

`result` is always a dict, never an exception:

```python
{"success": bool, "ran": bool, "error": str|None,
 "output": str, "result": {...}|None, "actions": int}
```

`ran` is the important one. `ran=False` means nothing started — no Blender
configured, an action the templates do not implement, a leaf value that is the
wrong kind of thing, or the destructive-run guard below. A result that did not
run never claims otherwise.

### Not emptying somebody's file

Recipes open with `clear_scene`. That is right for the ordinary run, which
starts from an empty Blender and removes only the default cube, camera and
light. It is wrong when a caller passes `blend_file` to open existing work
**and** the run saves back over it: Blender loads their file, the script empties
it, and `save_file` writes the empty result on top with nothing to undo.

`run_actions` refuses that combination before starting Blender, and says what
would have happened. Pass `allow_clearing_saved_file=True` to mean it.

The guard is deliberately narrow — loss needs all three of *open*, *clear*, and
*save back to the same path*. Opening a file, clearing it and exporting an FBX
touches nothing on disk and is allowed; so is saving to a different file, and so
is modifying and saving without clearing. Paths are compared after resolving and
normalising case, because `MyWork.blend` and `sub/../MyWork.blend` are the same
file.

No sentence can reach this: `map_text` never emits `save_file`, which is what
makes the hazard narrow rather than urgent. It is a guard for programmatic
callers.

**One run is one script.** Blender takes seconds to start and begins each
invocation with an empty file, so twenty actions as twenty invocations would be
twenty cold starts *and* twenty separate scenes. Objects made at step 1 are
there at step 20.

Named helpers exist for every action (`actions.add_cube(size=2)`,
`actions.rotate("Cube", x=90)`) and are single-action `run_actions` calls.

### Where Blender is

`ARIA_BLENDER_PATH` first, then the Blender plugin's `blender_path` setting.
The plugin must be installed and enabled; a disabled plugin is a refusal, not a
silent skip.

## Natural language

```python
from backend.blender import blender_nl_mapping as mapping

plan = mapping.map_text("Create a red low-poly car in Blender")
# {"actions": [...], "matched": ["recipe:car"], "summary": "create Car_Body, ..."}
```

Recipes: car, character, tree, house, table, chair, sword, barrel, rock (plus
synonyms). Operations: rig, animate (walk/idle), unwrap, sculpt, export, smooth,
mirror. It reads colours, numbers, `low-poly`, and `called X`.

**Blender has to be named.** "in Blender", "with Blender", "using Blender",
"from Blender", "use Blender to…", or "Blender, …" to open. `"Make a car"` on
its own returns `None` — it could mean a 3D model, a game object, a drawing, or
nothing in particular, and acting on it would start a subprocess and write files
because somebody used a common verb. Naming the tool costs the person two words
and removes the guess entirely.

Naming a different asset tool (Ludo.ai) returns `None` even when Blender is also
named, because an ambiguous sentence is a reason to ask rather than to pick. The
match is on the bare word, not on "in Ludo" — `"make a car in Blender or Ludo"`
names two tools with only one preposition, and that is exactly the sentence that
should not be guessed at. Unity is deliberately *not* treated as a competing
tool: `"export it from Blender to Unity"` is a Blender request with a
destination, and the asset pipeline exists for it.

**`None` means "not recognised", and callers must treat it as a conversation,
not a guess.** A wrong match is worse than no match: no match is a chat message,
a wrong match is somebody wondering why their car is a torus.

Questions never build. `"how do I model a car in Blender?"` returns `None` —
found during testing, when it built a 27-step car because the verb list contains
"model".

A polite interrogative is not one of those. `"can you make a car in Blender?"`
builds a car: it is a request in a polite shape, and once the tool gate has done
its job there is no ambiguity left for a question guard to protect against — the
person named Blender.

What still does not build is a request to be *taught*, and those are separated
by what they ask for rather than by how they open:

| Sentence | Result |
| --- | --- |
| `"can you make a car in Blender?"` | builds |
| `"can you tell me how to make a car in Blender?"` | `None` |
| `"how do I make a car in Blender?"` | `None` |
| `"what's the best way to make a house in Blender"` | `None` |

So there are two guards: one for a sentence that *opens* with an interrogative
about the world (`how`, `what`, `is`), and one for a phrase asking to be taught
(`tell me`, `explain`, `how to`, `the best way`, `tutorial`) wherever it sits.
Dropping `can|could|would|will` from the first without adding the second would
have let `"can you tell me how to make a car in Blender?"` build 27 steps.

`names_blender(text)` and `names_another_tool(text)` are public, so routing and
running can ask the same question — a short-circuit that decided differently
from the mapper would be a bug waiting for a sentence that landed between them.

## Into Unity

```python
from backend.blender import blender_asset_pipeline as pipeline

pipeline.deliver_to_unity("D:/out/car.fbx", place=True, prefab=True)
```

Steps are reported separately, because "the import worked and the placement did
not" is a true and useful sentence and one success flag over four steps is not.

Parameter names were read out of the Pipeline package's own source in the
project rather than guessed — four earlier guesses about this CLI were wrong:

| Command | Source | Parameters |
| --- | --- | --- |
| `import_asset` | `Editor/Commands/Assets/AssetCommands.cs` | `source`, `path`, `confirm`, `dry_run` |
| `create_prefab` | `Editor/Commands/Prefabs/PrefabCommands.cs` | `source`, `path` |
| `instantiate_prefab` | `Editor/Commands/Prefabs/PrefabCommands.cs` | `prefab`, `scene_path`, `name` |

Two consequences worth knowing:

- **`create_prefab` takes a scene GameObject, not an asset.** A freshly imported
  model has to be placed first; `deliver_to_unity` does that in order.
- **`import_asset` does its own copy**, so the pipeline does not copy first —
  `File.Copy` onto itself throws.

Everything except the copy needs the Editor running. With it closed,
`trigger_unity_import` falls back to a plain filesystem copy and says so;
`deliver_to_unity` then stops rather than claiming a prefab it did not make.

## What headless Blender cannot do

Stated here because the alternative is a template that lies.

- **Sculpt strokes.** A stroke needs screen coordinates and a 3D viewport;
  `--background` has neither. `sculpt_brush` enters sculpt mode, sets strength
  and dyntopo detail size, and reports what it did.
- **Choosing a brush.** `bpy.data.brushes` holds exactly one entry (`Draw`) —
  since 4.3 the rest are asset-library brushes a headless session never loads —
  and `tool_settings.sculpt.brush` is **read-only**. The result reports
  `requested`, `active` and whether they match.

## Version notes for Blender 5.0.1

Measured against the real 5.0.1 (`a3db93c5b259`), not recalled:

- `Material.use_nodes` is deprecated and warns **on read as well as write**. New
  materials already arrive with a node tree and a Principled BSDF, so the
  templates go through `node_tree` and never touch the property.
- `mesh.use_auto_smooth` does not exist (removed in 4.1).
- `wm.obj_export` is the current OBJ exporter.
- Metallic and roughness live on the Principled BSDF node, not the material.
- `io_scene_fbx` and `io_scene_gltf2` are enabled by default.
- Runs use `--factory-startup` so a user's add-ons cannot change the result.

## Tests

`backend/tests/test_blender_actions.py` — 197 tests, plus 22 in
`backend/tests/test_plugin_output_dir.py` for the output-folder setting, no Blender required. They
compile every generated script (the check that would have caught `def main:`),
prove hostile names and paths cannot become code, and prove a refusal never
claims to have run.

They **cannot** prove an operator exists in 5.0.1. That was measured separately
by running all 39 actions against the real Blender in one script: 39/39 steps,
producing FBX, GLB, OBJ and .blend files, with no deprecation warnings.

## Wired into chat

`turn_orchestrator._blender_reply` sits with the other command short-circuits,
ahead of intent detection and model routing. It calls
`blender_actions.answer_request(text)`, which returns `None` for anything that
is not a Blender request — including questions *about* Blender, which want a
model and get one.

The failure it exists for, measured with the plugin installed, enabled and
pointing at a working Blender 5.0.1:

```
create a car for me in Blender
  -> phi-3-mini: "I can guide you through creating a car in Blender.
                  What specific features would you like for your car model?"
```

Nothing ran. Every piece needed to build that car existed and was tested;
nothing called any of it. A model that cannot build something does not say so —
it offers to help, which reads like progress and is not. Now:

```
create a car for me in Blender
  -> Built Car in Blender: 6 objects (Car_Body, Car_Cabin, Car_Wheel_1, …).

     - …/aria_output/blender/Car.fbx
     - …/aria_output/blender/Car.blend

     Steps: create Car_Body, Car_Cabin, … then bevel the edges, then add materials.
```

**Two gates stand before the subprocess, and both should stay:** the sentence
must name Blender, and the plugin must be installed and enabled. Neither is
inferred — naming the tool is the person's word, and enabling the plugin is a
deliberate act on its own page.

### Where the files go

`Documents/ARIA/Blender` by default, and settable per plugin on its config page
under Plugins ("Output folder"). Never the project source tree — ARIA does not
write there outside a commit — and never the user's Unity project, because
putting a file in somebody's game on every chat message is not a default anyone
chose. `deliver_to_unity` sends it on when they ask.

A folder that does not exist yet is the normal answer: it is created on first
use. What is refused is a path that cannot be made — a file sitting in the way,
a relative path, or a drive that is not there.

Resolution is `plugin_settings.output_dir(plugin_id)`, shared by every plugin
that produces files: environment (`ARIA_BLENDER_OUTPUT`) first, then the
plugin's own `output_dir`, then the default. Two copies of "where do the files
go" drift, and the one nobody updated is the one somebody is using.

Every request writes **two** files: a `.blend` so the work can be opened and
carried on, and a model file in whichever format the sentence asked for, or FBX,
which is what Unity wants. A plan on its own builds in memory and Blender then
exits — the run would report success and leave nothing on disk. Names never
collide: a second Car becomes `Car_2`.

### When it cannot help

A sentence that asks for something outside the recipe list gets an honest answer
rather than a model's description of a spaceship:

```
make me a spaceship in Blender
  -> I did not build anything, because I do not know how to make that yet.

     I can build: barrel, car, chair, character, house, rock, sword, table, tree…
     I can also rig, animate a walk or idle, UV unwrap, set up sculpting,
     smooth, mirror and export.
```

That distinction — a request I cannot fill, versus a question I should not
answer — is `blender_nl_mapping.wants_something_built()`.
