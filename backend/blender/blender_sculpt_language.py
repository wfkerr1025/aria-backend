"""ARIA Lite - sculpting in plain English.

"Make the nose bigger", "pull the chin out a little", "give him a smile".

A sentence becomes sculpt_stroke actions aimed at LANDMARKS -- named
places on the model (find_landmarks marks a head; set_landmark marks
anything) -- so nothing here needs to know where the nose is, only
that it is called "nose". Each change is a small, fixed recipe of
strokes, sized to the feature: "bigger" on a nose and "bigger" on a
cheek are the same words and very different brushes.

WHAT THIS IS NOT
Not a model guessing at bpy, and not taste. It is a vocabulary: the
changes a sculptor names most, done the way a sculptor does them. Words
it does not know come back as None, so the sentence goes on to be
answered rather than half-done.

Expressions -- smile, frown, open mouth, surprised, angry -- are made as
shape keys, not sculpted into the face, so they can be dialled from 0
to 1 and animated, and the neutral face is never lost.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["plan", "EXPRESSIONS", "TARGETS"]

# Landmark names by the words people use for them. A paired feature
# ("_l"/"_r") is both sides unless the sentence says which.
TARGETS: Dict[str, Tuple[str, ...]] = {
    "nose": ("nose",),
    "chin": ("chin",),
    "jaw": ("jaw", "jaws", "jawline", "jaw line"),
    "cheek": ("cheek", "cheeks", "cheekbone", "cheekbones", "cheek bones"),
    "eye": ("eye", "eyes", "eye socket", "eye sockets", "sockets"),
    "ear": ("ear", "ears"),
    "brow": ("brow", "brows", "eyebrow", "eyebrows", "brow ridge"),
    "forehead": ("forehead",),
    "mouth": ("mouth", "lips", "lip"),
    "crown": ("top of the head", "top of his head", "top of her head", "top of its head",
              "crown", "skull"),
    "back_of_head": ("back of the head", "back of his head", "back of her head",
                     "back of its head"),
    # The body (find_landmarks kind="body").
    "neck": ("neck",),
    "shoulder": ("shoulder", "shoulders"),
    "chest": ("chest", "torso"),
    "pec": ("pec", "pecs", "pectoral", "pectorals", "breast", "breasts"),
    "belly": ("belly", "stomach", "gut", "tummy", "abs", "abdomen"),
    "waist": ("waist", "waistline"),
    "hip": ("hip", "hips"),
    # Only with an owner: "pull the chin back" is a direction, not a spine.
    "back": ("the back", "his back", "her back", "its back", "their back",
             "upper back", "lower back"),
    "buttock": ("butt", "buttock", "buttocks", "bum", "glutes", "backside", "rear end"),
    "arm": ("arm", "arms"),
    "upper_arm": ("upper arm", "upper arms", "bicep", "biceps"),
    "elbow": ("elbow", "elbows"),
    "forearm": ("forearm", "forearms"),
    "wrist": ("wrist", "wrists"),
    "hand": ("hand", "hands"),
    "leg": ("leg", "legs"),
    "thigh": ("thigh", "thighs"),
    "knee": ("knee", "knees"),
    "calf": ("calf", "calves"),
    "shin": ("shin", "shins"),
    "ankle": ("ankle", "ankles"),
    "foot": ("foot", "feet"),
}
PAIRED = frozenset({"jaw", "cheek", "eye", "ear", "shoulder", "pec", "waist", "hip", "buttock",
                    "arm", "upper_arm", "elbow", "forearm", "wrist", "hand", "leg", "thigh",
                    "knee", "calf", "shin", "ankle", "foot"})

# How a pair is said in a reply: "the calves", not "the calfs".
PLURALS = {"calf": "calves", "foot": "feet", "waist": "waist", "belly": "belly",
           "mouth_corner": "mouth corners", "upper_arm": "upper arms"}

# A word for a whole limb means its landmark: "bigger arms" is the upper arm.
ALIASES = {"arm": "upper_arm", "leg": "thigh"}

# "Longer" on a limb is a stretch toward its end, along the limb's own
# line -- not a bump off its side, which is what "longer" means for a
# nose or a chin.
LIMB_ENDS = {"arm": "hand", "upper_arm": "hand", "forearm": "hand", "hand": "hand",
             "leg": "ankle", "thigh": "ankle", "shin": "ankle", "calf": "ankle"}

# A width change on a feature in the middle of the body is made on the
# pair either side of it: a chest is broadened at the pecs, a belly
# narrowed at the waist.
WIDTH_VIA = {"chest": "pec", "belly": "waist"}

# How much wider than its own landmark a brush on this feature should
# reach. A chin is not a knob on the face: pulling only the landmark's
# own patch forward grew a ball under the mouth (measured, in the clay
# preview); the whole lower jaw has to come with it.
SIZE_BIAS = {"chin": 1.7, "jaw": 1.3, "back_of_head": 1.3, "crown": 1.2,
             "belly": 1.4, "back": 1.3, "chest": 1.2, "buttock": 1.2, "thigh": 1.2,
             "shoulder": 1.2}

# Amount words: how much of a recipe's normal strength.
_LITTLE = re.compile(r"\b(?:slightly|a (?:little|bit|touch|tad)|a little bit|subtly|just a bit)\b", re.I)
_LOT = re.compile(r"\b(?:a lot|much|very|really|way|a great deal|heavily|dramatically)\b", re.I)


def _stroke(landmark: str, brush: str, **params: Any) -> Dict[str, Any]:
    return {"brush": brush, "landmark": landmark, **params}


# Each change: the words for it, and the strokes it is -- written for one
# landmark and an amount (1.0 = the ordinary amount).
CHANGES: List[Tuple[str, "re.Pattern[str]", Any]] = [
    ("bigger", re.compile(r"\b(?:bigger|larger|enlarge[sd]?|grow|fuller|bulkier|plumper|"
                          r"thicker|beefier|more muscular|stronger|bulk(?:ed)? up|"
                          r"puff(?:ier)?|more prominent)\b", re.I),
     lambda lm, a: [_stroke(lm, "inflate", strength=0.7 * a, size=1.1),
                    _stroke(lm, "draw", strength=0.5 * a),
                    _stroke(lm, "smooth", strength=0.4, size=1.3)]),
    ("smaller", re.compile(r"\b(?:smaller|shrink|reduce[sd]?|less prominent|tone down|"
                           r"thinner|slimmer|skinnier|leaner)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="in", distance=0.3 * a, size=1.2),
                    _stroke(lm, "smooth", strength=0.4, size=1.3)]),
    ("longer", re.compile(r"\b(?:longer|stick(?:s|ing)?(?: \w+){0,3} out|pull(?:s|ed)?(?: \w+){0,3} out|"
                          r"further out|out more|protrude|jut(?:ting)? out)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="out", distance=0.5 * a, size=1.2)]),
    ("pointier", re.compile(r"\b(?:pointier|pointy|sharper|pointed)\b", re.I),
     lambda lm, a: [_stroke(lm, "pinch", strength=0.6 * a, size=0.9),
                    _stroke(lm, "grab", direction="out", distance=0.2 * a, size=0.7)]),
    ("flatter", re.compile(r"\b(?:flatter|flatten(?:ed)?|flat)\b", re.I),
     lambda lm, a: [_stroke(lm, "flatten", strength=min(0.9 * a, 1.0), size=1.1)]),
    ("smoother", re.compile(r"\b(?:smoother|smooth(?:en)?|soften|softer)\b", re.I),
     lambda lm, a: [_stroke(lm, "smooth", strength=min(0.8 * a, 1.0), size=1.2)]),
    ("wider", re.compile(r"\b(?:wider|widen|broader|broaden)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="outward", distance=0.4 * a, size=1.3)]),
    ("further back", re.compile(r"\b(?:(?:pull|push|move|tuck)(?:s|ed)?(?: \w+){0,3} back|"
                                r"recede|receding)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="back", distance=0.4 * a, size=1.2)]),
    ("narrower", re.compile(r"\b(?:narrower|narrow)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="inward", distance=0.35 * a, size=1.3)]),
    ("higher", re.compile(r"\b(?:raise[sd]?|lift(?:ed)?|higher|move[sd]? up|push(?:ed)? up)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="up", distance=0.4 * a, size=1.2)]),
    ("lower", re.compile(r"\b(?:lower(?:ed)?|drop(?:ped)?|move[sd]? down|push(?:ed)? down)\b", re.I),
     lambda lm, a: [_stroke(lm, "grab", direction="down", distance=0.4 * a, size=1.2)]),
    ("deeper", re.compile(r"\b(?:deeper|deepen|sunken|sink|hollow(?:er)?|dent|push(?:ed)? in|"
                          r"set (?:further )?back)\b", re.I),
     lambda lm, a: [_stroke(lm, "draw", strength=-2.5 * a, size=1.0),
                    _stroke(lm, "smooth", strength=0.3, size=1.1)]),
]

# Expressions: shape keys, each a few grabs on the face's landmarks.
EXPRESSIONS: Dict[str, Tuple["re.Pattern[str]", List[Dict[str, Any]]]] = {
    "Smile": (re.compile(r"\b(?:smil(?:e|es|ing)|grin(?:ning|s)?|happy)\b", re.I), [
        _stroke("mouth_corner_l", "grab", direction="up outward back", distance=0.6,
                size=1.6, mirror="X"),
        _stroke("cheek_l", "grab", direction="up", distance=0.2, size=0.9, mirror="X"),
    ]),
    "Frown": (re.compile(r"\b(?:frown(?:ing|s)?|sad|glum|unhappy)\b", re.I), [
        _stroke("mouth_corner_l", "grab", direction="down", distance=0.6, size=1.6, mirror="X"),
        _stroke("brow", "grab", direction="down", distance=0.12, size=0.8),
    ]),
    "Mouth_Open": (re.compile(r"\b(?:open(?:s|ed|ing)? (?:\w+ )?mouth|mouth open|jaw drop)\b", re.I), [
        _stroke("chin", "grab", direction="down", distance=0.45, size=1.6),
        _stroke("mouth", "draw", strength=-2.0, size=0.9),
    ]),
    "Surprised": (re.compile(r"\b(?:surprised?|shocked|astonished|amazed)\b", re.I), [
        _stroke("brow", "grab", direction="up", distance=0.3, size=0.9),
        _stroke("chin", "grab", direction="down", distance=0.25, size=1.5),
        _stroke("mouth", "draw", strength=-1.5, size=0.7),
    ]),
    "Angry": (re.compile(r"\b(?:angry|anger|furious|scowl(?:ing|s)?|cross)\b", re.I), [
        _stroke("brow", "grab", direction="down back", distance=0.25, size=0.7),
        _stroke("mouth_corner_l", "grab", direction="down", distance=0.25, size=1.3, mirror="X"),
    ]),
}
_EXPRESSION_ASK = re.compile(
    r"\b(?:give|gives|giving|add|make|let)\b.*\b(?:a |an )?(?:expression|look|face)?|"
    r"\b(?:look|looking|looks)\b", re.I)


def _amount(text: str) -> float:
    if _LITTLE.search(text):
        return 0.5
    if _LOT.search(text):
        return 1.8
    return 1.0


def _targets(text: str) -> List[str]:
    """Landmark names in a clause, sides resolved."""
    found: List[Tuple[int, str]] = []
    taken = []
    lowered = text.lower()
    # Longest phrases first, so "back of the head" is not also "head".
    phrases = sorted(((w, t) for t, words in TARGETS.items() for w in words),
                     key=lambda pair: -len(pair[0]))
    for word, target in phrases:
        for match in re.finditer(r"\b" + re.escape(word) + r"\b", lowered):
            # Overlapping at all, not just starting inside: "the back" sits
            # across the front edge of "back of the head".
            if any(match.start() < b and match.end() > a for a, b in taken):
                continue
            taken.append((match.start(), match.end()))
            found.append((match.start(), target))
    names: List[str] = []
    for start, target in sorted(found):
        if target in PAIRED:
            before = lowered[max(0, start - 12):start]
            if re.search(r"\bleft\b", before):
                names.append(f"{target}_l")
            elif re.search(r"\bright\b", before):
                names.append(f"{target}_r")
            else:
                names.append(f"{target}_l*")          # both sides: mirrored
        else:
            names.append(target)
    return list(dict.fromkeys(names))


def plan(text: str) -> Optional[Dict[str, Any]]:
    """Sculpt steps for a sentence, or None if it is not one this knows.

    Returns {"strokes": [...], "expression": name or None, "summary": str}.
    Each stroke is sculpt_stroke params without the object, which the
    caller knows. Clauses split on "and", commas and "then", and a
    clause with a target but no change borrows the change that follows
    it ("make the nose and chin bigger").
    """
    said = str(text or "")

    for name, (words, strokes) in EXPRESSIONS.items():
        if words.search(said) and (_EXPRESSION_ASK.search(said) or len(said.split()) < 8):
            scaled = []
            amount = _amount(said)
            for stroke in strokes:
                s = dict(stroke)
                if "distance" in s:
                    s["distance"] = round(s["distance"] * amount, 3)
                if "strength" in s:
                    s["strength"] = round(s["strength"] * amount, 3)
                scaled.append(s)
            return {"strokes": scaled, "expression": name,
                    "summary": f"a {name.replace('_', ' ').lower()} expression"}

    clauses = [c for c in re.split(r"\s*(?:,|;|\bthen\b|\band\b)\s*", said) if c.strip()]
    strokes: List[Dict[str, Any]] = []
    done: List[str] = []
    waiting: List[str] = []
    for clause in clauses:
        targets = _targets(clause)
        change = next(((label, recipe) for label, words, recipe in CHANGES
                       if words.search(clause)), None)
        if change is None:
            waiting.extend(targets)
            continue
        targets = waiting + targets
        waiting = []
        if not targets:
            continue
        label, recipe = change
        amount = _amount(clause)
        for target in targets:
            both = target.endswith("*")
            name = target.rstrip("*")
            side = name[-1] if name.endswith(("_l", "_r")) else None
            base = name[:-2] if side else name
            if label in ("wider", "narrower") and base in WIDTH_VIA:
                base = WIDTH_VIA[base]
                if side is None:
                    side, both = "l", True
            if label == "longer" and base in LIMB_ENDS:
                end = f"{LIMB_ENDS[base]}_{side or 'l'}"
                limb = [_stroke(end, "grab", direction="along", distance=round(0.8 * amount, 3),
                                size=2.4)]
                if both:
                    limb[0]["mirror"] = "X"
                strokes.extend(limb)
                done.append(f"{base.replace('_', ' ')}{'s' if both else ''} longer"
                            + (" (a little)" if amount < 1 else " (a lot)" if amount > 1 else ""))
                continue
            base = ALIASES.get(base, base)
            landmark = f"{base}_{side}" if side else base
            bias = SIZE_BIAS.get(base, 1.0)
            for stroke in recipe(landmark, amount):
                stroke["size"] = round(stroke.get("size", 1.0) * bias, 3)
                for key in ("strength", "distance"):
                    if key in stroke:
                        stroke[key] = round(stroke[key], 3)
                if both:
                    stroke["mirror"] = "X"
                strokes.append(stroke)
            shown = base.replace("_", " ")
            if both:
                shown = PLURALS.get(base, shown + "s")
            elif side:
                shown += " (left)" if side == "l" else " (right)"
            done.append(f"{shown} {label}"
                        + (" (a little)" if amount < 1 else " (a lot)" if amount > 1 else ""))
    if not strokes:
        return None
    return {"strokes": strokes, "expression": None, "summary": "; ".join(done)}
