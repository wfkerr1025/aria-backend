"""ARIA Lite - what the global rules mean when the project is Unity.

The global brief asks for eight properties and names no mechanism, on
purpose: "serialize it" is right everywhere and "use JsonUtility" is
right in exactly one place. This is that one place.

Nothing here redefines a global rule. Every line answers a question the
global brief already asked:

    global: "let its state be saved and loaded back"
    here:   "[Serializable] data classes, JsonUtility.ToJson/FromJson,
             Application.persistentDataPath"

    global: "expose hooks so other code can respond to it"
    here:   "UnityEvent for the inspector, C# event Action<T> for code"

WHY THIS ONE IS WORTH BUILDING FIRST
------------------------------------
Measured on the global brief alone: "create a player_inventory.cs file
with a full inventory script" produced 63 lines with four methods,
guard clauses and typed exceptions -- a real improvement on the nine
lines it replaced, and still not something you could drop into a Unity
project. No MonoBehaviour, no serialization, no events, nothing the
engine could see.

A concrete mechanism lands where an abstract property does not. That is
the whole bet of the plugin layer and this is the first test of it.

WHAT IT COSTS, AND THE TRADE IT MAKES
-------------------------------------
Also measured: adding Unity conventions moved the model TOWARD idiom and
AWAY from completeness -- 29 lines, two methods, events and
[Serializable], and the validation gone. Attention is finite on a 12B
and instructions compete.

So this plugin is written to spend its words on what the global brief
cannot say, and the global completeness rule is repeated after it (see
tool_brief._CLOSING). The measurement of whether that holds is in the
commit message, not in a claim here.

ACTIVATION
----------
On intent, not on a filename. The brief is built before the model has
proposed a path, so the only evidence available is what the user said --
and a Unity request usually says so: "unity", "monobehaviour",
"scriptableobject", "prefab", "c# script".

".cs" is included, and that is a deliberate over-reach worth naming: a
plain .NET console app would get Unity idioms it does not want. It is
here because the target prompt for this work -- "create a
player_inventory.cs file with a full inventory script" -- contains no
Unity word at all, and a plugin that missed the case it was built for
would be a plugin nobody could use. On a machine where .cs means
something else, drop that one token.
"""

from __future__ import annotations

from backend.core.brief_plugins import BriefPlugin, register_plugin

from logger import get_logger

logger = get_logger(__name__)

__all__ = ["PLUGIN", "NAME", "register"]

NAME = "unity_csharp"

# What the user says when they mean Unity.
#
# Word-matched, not substring-matched: "unity" inside "opportunity" and
# "community" fired this plugin before brief_plugins learned the
# difference.
INTENT_HINTS = (
    "unity",
    "monobehaviour",
    "scriptableobject",
    "unityevent",
    "serializefield",
    "gameobject",
    "prefab",
    "coroutine",
    "c#",
    "csharp",
    ".cs",
)

# Every line answers a question the global brief asked, in Unity's terms.
# Kept tight: MAX_PLUGIN_CHARS is 1200 and a plugin that fills it is
# spending the window the model needs for the file itself.
IDIOMS = (
    "MonoBehaviour for anything that lives on a GameObject; "
    "ScriptableObject for shared data assets; [SerializeField] private "
    "fields so the inspector can see them without making them public; "
    "no work in Update() that a callback can do instead."
)

SERIALIZATION = (
    "[Serializable] data classes and JsonUtility.ToJson/FromJson, saved "
    "under Application.persistentDataPath. JsonUtility does not serialize "
    "Dictionary, so hold saveable state in a List of [Serializable] entries."
)

EVENTS = (
    "UnityEvent fields for designers to wire in the inspector, and C# "
    "event Action<T> for code. Raise one whenever state changes, so UI can "
    "redraw without polling."
)

TESTS = (
    "NUnit through the Unity Test Framework, in an EditMode test assembly, "
    "one [Test] per behaviour. Test the plain C# parts directly; keep "
    "MonoBehaviour logic thin enough to test without a scene."
)

# The rules that are Unity's and belong to no global property.
RULES = (
    "Use Debug.LogWarning and Debug.LogError rather than exceptions for "
    "recoverable problems, so play mode continues and the message is in "
    "the console.",
    "Never assume a serialized reference is set: null-check it and log "
    "which field is missing.",
    "Write /// <summary> on every public member -- the inspector and "
    "IntelliSense both read it.",
)

PLUGIN = BriefPlugin(
    name="Unity C#",
    idioms=IDIOMS,
    serialization=SERIALIZATION,
    events=EVENTS,
    tests=TESTS,
    rules=RULES,
    applies_to=INTENT_HINTS,
)


def register() -> None:
    """Make this plugin available to the brief."""
    register_plugin(PLUGIN)
