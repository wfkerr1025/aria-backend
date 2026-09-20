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
