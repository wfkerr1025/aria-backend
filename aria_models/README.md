# aria_models -- the base model library

A recipe in `aria_recipes/` says how to add detail. A base model is the
thing detail gets added TO. They are deliberately separate, and the
separation is the point.

## Why base models are not generated

Both ways the first character experiment failed were the base model's
absence, and neither was fixable by writing a better recipe:

* A cube scaled to a torso has **eight vertices**. You cannot sculpt on
  eight vertices, and subdividing gives a rounded pillow, not a chest.
  Real sculpting needs edge loops where the form bends, and no amount
  of templated primitive-stacking puts them there.
* A painting of a face has **no correspondence to a sphere's UVs**.
  Projecting `head_steadyA.png` onto a smart-unwrapped sphere rendered a
  solid black ball, because most of a cut-out is transparent and the
  unwrap had no idea where a face was meant to go.

So bases are authored once -- made, bought or downloaded -- with clean
quad topology and sensible UVs, and ARIA layers detail onto them. For
props (rocks, crates, carts, tools) primitives are still the right
answer and `aria_recipes/blender/` builds those procedurally.

## What is here

| file | what |
| --- | --- |
| `manifest_human_base_meshes.json` | where the bundle came from, its licence, its checksum, and every base worth using with its measured poly count |
| `blender/human/human_base_meshes_bundle.blend` | the bundle itself, 49 MB |
| `blender/human/thumbnails/` | one picture per asset |
| `fetch_human_base_meshes.py` | re-downloads and verifies the .blend against the manifest's sha256 |
| `landmarks_human_base_meshes.json` | where each body's parts are -- ankle, knee, crotch, waist, chest, armpit, neck, chin, crown -- in the world metres a recipe sees |
| `measure_base_landmarks.py` | re-derives that table by slicing the bodies in Blender |

## Why the landmarks are measured and not estimated

A garment recipe is a box in world metres: keep the body between 0.97
and 1.47 and what is left is a vest. Those numbers are the whole
recipe, and being three centimetres out puts the trousers above the
navel. The first four garments were cut with numbers read off a
viewport by eye and every one of them took three passes to stop being
a crop top.

So the bodies are sliced instead, every 5mm, and the landmarks fall
out of the shape: the crotch is where two leg-shaped clusters become
one torso, the armpit is the top of the run where a slice cuts left
arm, torso and right arm, the waist is the narrowest torso between
them. Recipes then name a landmark -- `z_min: waist` -- rather
than carrying a number, so one pair of trousers fits every body here
instead of only the one it was measured against.

    python aria_models/measure_base_landmarks.py

It needs Blender and the .blend, and it rewrites the table in place.

## The .blend is not in git

49 MB of binary would sit in the history forever, and a binary is easy
to add later and very hard to remove once committed. The manifest and
the fetch script are committed instead, so any clone can reproduce the
library exactly:

    python aria_models/fetch_human_base_meshes.py

If you would rather have it tracked, delete its line from
`.gitignore` and commit it -- that decision is reversible in the
direction that matters.

## Licence

CC0, from Blender Studio. Free for commercial use, no attribution
required. The manifest records the source URL and the retrieval date.
