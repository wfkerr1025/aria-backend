"""Measure the base bodies, and write down where their parts are.

    python aria_models/measure_base_landmarks.py

WHY THIS EXISTS
---------------
A garment recipe is a BOX in world metres: keep the body between z 0.97
and z 1.47, throw the rest away, and what is left is a vest. Those
numbers are the whole recipe. Get the waist wrong by three centimetres
and the trousers start above the navel.

The first four garments were cut with numbers read off a viewport by
eye, and every one of them needed three passes to stop being a crop
top. Eyeballing a figure is a slow way to find out that its waist is at
1.07 and not the 1.02 somebody wrote in a docstring -- so this measures
instead, and the recipes quote the measurement.

HOW
---
It runs the REAL base pipeline -- append, scale to height, apply
transforms, origin to floor -- through `blender_script_templates`, so
the profile is the world the recipe's boxes will actually be read in.
Measuring the mesh as it sits in the bundle would describe a different
body: unscaled, and its origin somewhere in its middle.

Then it takes a reading every 5mm and, for each, splits the vertices
into clusters across x. That split is what makes the landmarks findable
without a human looking at them:

    two clusters   legs, with a gap between them
    three clusters left arm, torso, right arm -- the figures are A-posed
    one cluster    torso with the arms merged into it, or a head

So the crotch is where two clusters become one, the armpit is the top
of the three-cluster run, and the waist is the narrowest torso between
them. Every landmark below is derived that way rather than typed in.

WHAT IT WRITES
--------------
`landmarks_human_base_meshes.json` -- per base: the landmark heights,
the half-widths at them, and which way the figure faces. Recipes name
those landmarks ("waist", "knee-0.02") instead of carrying numbers, so
one pair of trousers fits every body in the library rather than only
the one it was measured against.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

MANIFEST = os.path.join(HERE, "manifest_human_base_meshes.json")
OUT = os.path.join(HERE, "landmarks_human_base_meshes.json")

# 5mm between readings, each taken over a 20mm band around it. The two
# numbers are separate on purpose and the reason was measured: at 5mm a
# slice of a 12,500-vertex body holds about thirty vertices, and thirty
# vertices scattered round a leg is enough to report a knee 0.4mm
# across and a crotch up inside the skull. Overlapping bands give a
# reading every 5mm, each backed by about a hundred vertices.
STEP = 0.005
BAND = 0.020

# A cluster smaller than this is not a limb, and gets merged back into
# whichever neighbour it sits closest to. This is what makes the same
# reading work on both families of base: the realistic bodies carry a
# third of the stylised ones' vertices through the torso, so a 20mm
# band catches loop segments 32mm apart and splits one chest into four
# slivers of five to seven vertices. Merging those back gives one
# chest; a fatter gap threshold would instead have glued the A-posed
# arms to the ribs, where the real gap closes to 35mm.
MIN_CLUSTER = 12

# Two vertex columns further apart than this across x are different
# limbs. A thigh is ~0.11m across and the gap between the ankles is
# ~0.13m, so the split has to sit between those -- and at the shoulder
# the gap between an A-posed upper arm and the ribs closes to 35mm,
# which is what stops this being generous.
GAP = 0.03

# Which bases get measured, and at what height. Only the whole figures:
# a landmark table for a loose jaw describes nothing a garment can be
# cut from.
BODIES = [
    ("GEO-body_male_stylized", 1.80),
    ("GEO-body_female_stylized", 1.62),
    ("GEO-body_male_realistic", 1.69),
    ("GEO-body_female_realistic", 1.64),
]

# Bodies a RECIPE makes by reshaping a base -- the anime figures. They
# are measured as the recipe leaves them (base, then proportions), and
# filed under the recipe's name, which is what a garment recipe names
# in `base.recipe` to be cut from one.
RECIPE_BODIES = ["anime_male_body", "anime_female_body"]


# The measurement, as it runs inside Blender. Printed between markers
# because Blender's stdout also carries add-on banners and timings.
PROFILE = r'''
import json as _json

_m = [o for o in bpy.data.objects if o.type == "MESH"][0]
_pts = [_m.matrix_world @ _v.co for _v in _m.data.vertices]
_zs = [p.z for p in _pts]
_lo, _hi = min(_zs), max(_zs)

_rows = []
_step = %(step)s
_half = %(band)s / 2.0
_least = %(least)s
_n = int(round((_hi - _lo) / _step))
for _i in range(_n + 1):
    _z = _lo + _i * _step
    _band = [p for p in _pts if abs(p.z - _z) <= _half]
    if not _band:
        continue
    _xs = sorted(p.x for p in _band)
    _groups, _run = [], [_xs[0]]
    for _x in _xs[1:]:
        if _x - _run[-1] > %(gap)s:
            _groups.append(_run)
            _run = [_x]
        else:
            _run.append(_x)
    _groups.append(_run)
    # Merge anything too small to be a limb into whichever neighbour it
    # sits closer to, smallest first, until every cluster is a limb.
    while len(_groups) > 1:
        _worst = min(range(len(_groups)), key=lambda _i: len(_groups[_i]))
        if len(_groups[_worst]) >= _least:
            break
        if _worst == 0:
            _into = 1
        elif _worst == len(_groups) - 1:
            _into = _worst - 1
        else:
            _left = _groups[_worst][0] - _groups[_worst - 1][-1]
            _right = _groups[_worst + 1][0] - _groups[_worst][-1]
            _into = _worst - 1 if _left <= _right else _worst + 1
        _low, _high = min(_worst, _into), max(_worst, _into)
        _groups[_low:_high + 1] = [sorted(_groups[_low] + _groups[_high])]
    _ys = [p.y for p in _band]
    _rows.append({
        "z": round(_z, 4),
        "n": len(_band),
        "y": [round(min(_ys), 4), round(max(_ys), 4)],
        "groups": [[round(_g[0], 4), round(_g[-1], 4), len(_g)] for _g in _groups],
    })

print("ARIA_PROFILE_OPEN")
print(_json.dumps({"z_range": [round(_lo, 4), round(_hi, 4)],
                   "verts": len(_pts), "rows": _rows}))
print("ARIA_PROFILE_CLOSE")
'''


def _profile(blender, blend, body, height, recipe=None):
    """Run the base pipeline on one body and slice what comes out."""
    from backend.blender import blender_recipes as recipes
    from backend.blender import blender_script_templates as templates

    setup = ([{"action": "clear_scene"}] + recipes.base_actions(recipe)
             + recipes.proportions_actions(recipe)) if recipe else [
        {"action": "clear_scene"},
        {"action": "append_from_blend",
         "params": {"blend": blend, "object": body, "name": "Body",
                    "location": [0, 0, 0]}},
        {"action": "scale_to_height", "params": {"object": "Body", "height": height}},
        {"action": "apply_transforms", "params": {}},
        {"action": "origin_to_floor", "params": {}},
    ]

    script = (templates.build_script(setup) + "\n\n"
              + PROFILE % {"step": STEP, "band": BAND, "gap": GAP,
                           "least": MIN_CLUSTER})
    path = os.path.join(HERE, "_measure.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(script)

    try:
        run = subprocess.run(
            [str(blender), "--background", "--factory-startup", "--python", path],
            capture_output=True, text=True, timeout=900)
    finally:
        os.unlink(path)

    if "ARIA_PROFILE_OPEN" not in run.stdout:
        raise RuntimeError(
            f"{body or recipe}: Blender printed no profile (exit {run.returncode}).\n"
            + (run.stdout[-2000:] or "") + (run.stderr[-2000:] or ""))

    blob = run.stdout.split("ARIA_PROFILE_OPEN")[1].split("ARIA_PROFILE_CLOSE")[0]
    return json.loads(blob.strip())


def _central(row):
    """The cluster that straddles x=0 -- the torso, or the head."""
    for group in row["groups"]:
        if group[0] <= 0.0 <= group[1]:
            return (group[0], group[1])
    return None


def _raw_width(row):
    span = _central(row)
    return (span[1] - span[0]) if span else None


def _raw_leg(row):
    """How thick ONE leg is, from the outermost cluster in the slice.

    Below the crotch there is no central cluster at all -- both legs
    sit off to their own sides -- so a knee measured on the central
    cluster measures nothing, and a knee measured across BOTH legs
    finds the stance rather than the joint.
    """
    group = row["groups"][-1] if row["groups"] else None
    return (group[1] - group[0]) if group else None


def _smooth(rows, measure, window=5):
    """Each reading replaced by the median of itself and its neighbours.

    One reading in a hundred comes out wrong, and it comes out wrong in
    a way that matters: a band through the ribs found three clusters
    where there is one torso and called the chest 7cm across, so the
    narrowest point on the body -- the waist -- landed there. A median
    over five neighbours cannot be moved by one bad reading. A mean can.
    """
    values = [measure(row) for row in rows]
    out = []
    for index in range(len(values)):
        near = [v for v in values[max(0, index - window // 2):index + window // 2 + 1]
                if v is not None]
        out.append(sorted(near)[len(near) // 2] if near else None)
    return out


def _pick(rows, values, low, high, best):
    """The row between two heights whose smoothed reading `best` chooses."""
    band = [(row, value) for row, value in zip(rows, values)
            if value is not None and low <= row["z"] <= high]
    return best(band, key=lambda pair: pair[1])[0] if band else None


def _first_sustained(rows, above, holds, run=6):
    """Where a condition starts being true and keeps being true.

    One reading proves nothing: a single band can miss the gap between
    two legs, or find one through a nostril. Six together -- 3cm at the
    default step -- is a body part.
    """
    band = [row for row in rows if row["z"] >= above]
    for index, row in enumerate(band):
        window = band[index:index + run]
        if len(window) == run and all(holds(item) for item in window):
            return round(row["z"], 4)
    return round(above, 4)


def _last_sustained(rows, above, holds, run=6):
    """The top of the highest sustained run of a condition.

    The arms are found by looking for three clusters in a reading, and
    they stop being three at the shoulder -- but they have also not
    started yet just above the crotch, where the fingertips are still a
    few centimetres higher. Asking where three-clusters first STOPS
    answers "immediately", which put one figure's armpit at its hips.
    """
    band = [row for row in rows if row["z"] >= above]
    for index in range(len(band) - run, -1, -1):
        window = band[index:index + run]
        if all(holds(item) for item in window):
            return round(window[-1]["z"], 4)
    return round(above, 4)


def _first_dip(rows, values, above, below, rise=1.2):
    """The first narrow point in a profile that widens out again.

    Plain "narrowest between here and there" cannot find a neck: above
    the shoulders the body narrows to the neck, widens into the skull
    and narrows again to the crown -- and the crown is the narrowest of
    the three. So this walks up, remembers the thinnest reading so far,
    and calls it the dip once the body has widened back past it.
    """
    band = [(row, value) for row, value in zip(rows, values)
            if value is not None and above <= row["z"] <= below]
    best = None
    for row, value in band:
        if best is None or value < best[1]:
            best = (row, value)
        elif value > best[1] * rise:
            return best[0]
    return best[0] if best else None


def _landmarks(data):
    """Turn a sliced profile into named heights.

    Every one is read off the clustering and the width curve, never
    assumed from a fraction of the height: these bodies have heads a
    sixth of their length, so "the neck is at 0.85 of height" is wrong
    by a whole head on exactly the figures this library is for.
    """
    rows = data["rows"]
    floor, crown = data["z_range"]
    widths = _smooth(rows, _raw_width)
    legs = _smooth(rows, _raw_leg)

    def armed(row):
        return len(row["groups"]) >= 3

    # Crotch: the lowest height that stops being two separate legs and
    # STAYS one. "Stays" matters -- a band through the crown or the
    # ears also comes out as two clusters with a hole between them.
    crotch = _first_sustained(rows, floor, lambda row: _central(row) is not None)

    # The arms: A-posed, so between the fingertips and the shoulder a
    # reading cuts left arm, torso, right arm. The top of that run is
    # the armpit; the bottom of it is the fingertips.
    armpit = round(_last_sustained(rows, crotch, armed) + STEP, 4)
    fingertip = _first_sustained(rows, crotch, armed)

    knee = _pick(rows, legs, floor + (crotch - floor) * 0.55,
                 floor + (crotch - floor) * 0.80, min)
    calf = _pick(rows, legs, floor + (crotch - floor) * 0.30,
                 floor + (crotch - floor) * 0.60, max)
    ankle = _pick(rows, legs, floor + 0.02, floor + (crotch - floor) * 0.35, min)

    hip = _pick(rows, widths, crotch, crotch + (armpit - crotch) * 0.30, max)
    waist = _pick(rows, widths, hip["z"] if hip else crotch, armpit, min)
    # Stopping short of the armpit, because the widest torso reading
    # below it is the one where the arms are merging INTO it -- which
    # is the shoulder span, not the chest. Without the gap every body
    # reported a chest the width of its own shoulders.
    chest = _pick(rows, widths, waist["z"] if waist else crotch, armpit - 0.03, max)

    # Above the armpit everything is one cluster, so the neck has to be
    # found by shape rather than by size -- see _first_dip.
    neck = _first_dip(rows, widths, armpit + 0.02, crown)
    neck_z = neck["z"] if neck else armpit
    shoulder = _pick(rows, widths, armpit, neck_z, max)
    head = _pick(rows, widths, neck_z, crown, max)

    # Chin: coming up from the neck, the first reading half again as
    # wide as the neck is the jaw starting. On the realistic bases that
    # never happens -- their skull is only 1.50x their neck, so the
    # test lands exactly on its own threshold -- and there the chin
    # falls back to halfway between the neck and the widest part of the
    # head, which is where a jaw is.
    head_z = head["z"] if head else crown
    chin = round((neck_z + head_z) / 2.0, 4)
    if neck:
        thin = widths[rows.index(neck)]
        for row, value in zip(rows, widths):
            if neck_z < row["z"] < head_z and value and value > thin * 1.5:
                chin = round(row["z"], 4)
                break

    # The hand, for gloves: how far out the arm reaches, and how close
    # to the body the wrist end of it comes back.
    hand_top = fingertip + (armpit - fingertip) * 0.35
    hands = [row for row in rows
             if fingertip <= row["z"] <= hand_top and armed(row)]
    reach = max((row["groups"][-1][1] for row in hands), default=0.0)
    inner = min((row["groups"][-1][0] for row in hands), default=0.0)

    # Which way it faces. The foot runs further from the ankle on the
    # toe side, so the sign of the longer reach is the front.
    sole = [row for row in rows if row["z"] <= floor + 0.03]
    ankle_y = ankle["y"] if ankle else [0.0, 0.0]
    toe = (min(row["y"][0] for row in sole) - ankle_y[0]) if sole else 0.0
    heel = (max(row["y"][1] for row in sole) - ankle_y[1]) if sole else 0.0

    def at(row):
        return round(row["z"], 4) if row else None

    def half(row):
        """Half the torso's width at a height -- which is what a bound is."""
        value = widths[rows.index(row)] if row else None
        return round(value / 2.0, 4) if value else None

    def leg_half(row):
        value = legs[rows.index(row)] if row else None
        return round(value / 2.0, 4) if value else None

    return {
        "height": round(crown - floor, 4),
        "faces": "-Y" if abs(toe) > abs(heel) else "+Y",
        "z": {
            "sole": round(floor, 4),
            "ankle": at(ankle),
            "calf": at(calf),
            "knee": at(knee),
            "crotch": crotch,
            "hip": at(hip),
            "waist": at(waist),
            "chest": at(chest),
            "armpit": armpit,
            "shoulder": at(shoulder),
            "neck": neck_z,
            "chin": chin,
            "head": at(head),
            "crown": round(crown, 4),
            "fingertip": fingertip,
            "wrist": round(hand_top, 4),
        },
        "half_width": {
            "ankle": leg_half(ankle),
            "calf": leg_half(calf),
            "knee": leg_half(knee),
            "hip": half(hip),
            "waist": half(waist),
            "chest": half(chest),
            "shoulder": half(shoulder),
            "neck": half(neck),
            "head": half(head),
            "reach": round(reach, 4),
            "hand_inner": round(inner, 4),
        },
        "y": {
            "toe": round(min(row["y"][0] for row in sole), 4) if sole else None,
            "heel": round(max(row["y"][1] for row in sole), 4) if sole else None,
            "face": round(head["y"][0], 4) if head else None,
            "skull": round(head["y"][1], 4) if head else None,
            "chest_front": round(chest["y"][0], 4) if chest else None,
            "chest_back": round(chest["y"][1], 4) if chest else None,
        },
        "verts": data["verts"],
    }


def main():
    from backend.blender import blender_actions

    spec = json.loads(open(MANIFEST, encoding="utf-8").read())
    blend = os.path.join(HERE, spec["file"]["path"].replace("/", os.sep))
    if not os.path.isfile(blend):
        print("no bundle at", blend,
              "\nrun aria_models/fetch_human_base_meshes.py first", file=sys.stderr)
        return 2

    blender = blender_actions.blender_path()
    print("measuring with", blender)

    bases = {}
    for body, height in BODIES:
        print(" ", body, "at", height, "m ...", end=" ", flush=True)
        try:
            bases[body] = _landmarks(_profile(blender, blend, body, height))
        except (RuntimeError, ValueError) as error:
            print("FAILED")
            print("   ", error, file=sys.stderr)
            continue
        found = bases[body]["z"]
        print("ok -- crotch %s, waist %s, neck %s"
              % (found["crotch"], found["waist"], found["neck"]))

    for recipe in RECIPE_BODIES:
        print(" ", recipe, "(recipe) ...", end=" ", flush=True)
        try:
            bases[recipe] = _landmarks(_profile(blender, blend, None, None, recipe=recipe))
        except (RuntimeError, ValueError) as error:
            print("FAILED")
            print("   ", error, file=sys.stderr)
            continue
        found = bases[recipe]["z"]
        print("ok -- crotch %s, waist %s, neck %s"
              % (found["crotch"], found["waist"], found["neck"]))

    if not bases:
        print("nothing measured", file=sys.stderr)
        return 3

    out = {
        "name": "human_base_meshes_landmarks",
        "of": spec["name"],
        "units": "meters",
        "measured_by": "aria_models/measure_base_landmarks.py",
        "slice_step": STEP,
        "slice_band": BAND,
        "note": ("Heights are world metres on the body AS A RECIPE SEES IT: "
                 "appended, scaled to `height`, transforms applied, origin on "
                 "the floor. A recipe names these rather than carrying "
                 "numbers, so a garment cut for one body fits another."),
        "bases": bases,
    }
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
        handle.write("\n")
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
