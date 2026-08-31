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

from backend.core.brief_plugins import BriefPlugin, BriefTopic, register_plugin

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
# Kept tight: a plugin that fills its allowance is spending the window
# the model needs for the file itself.
IDIOMS = (
    "MonoBehaviour for anything that lives on a GameObject; "
    "ScriptableObject for shared data and assets; [SerializeField] on "
    "private fields so the inspector sees them without making them "
    "public; TryGetComponent over GetComponent when the result may be "
    "null; no work in Update() that a callback or coroutine can do."
)

SERIALIZATION = (
    "[Serializable] on EVERY data class, and JsonUtility.ToJson/FromJson "
    "for save and load, written under Application.persistentDataPath. "
    "JsonUtility cannot serialize a Dictionary, so hold saveable state in "
    "a List of [Serializable] entries."
)

EVENTS = (
    "UnityEvent fields for designers to wire in the inspector AND C# "
    "event Action<T> for code. Raise one on every state change, so UI "
    "redraws without polling."
)

TESTS = (
    "NUnit through the Unity Test Framework. EditMode for plain C# logic, "
    "PlayMode only when a frame or a scene is genuinely needed. One [Test] "
    "per behaviour, and keep MonoBehaviour logic thin enough to test "
    "without a scene."
)

# TRIED AND REVERTED: moving the doc rule to the very end.
#
# Doc comments were 2/2 before the container topic was added and 0/3
# after, and position looked like the cause -- the rule had not changed,
# only what now sat after it. So it was moved last, where recency would
# help it, and measured again:
#
#                       before the move   after
#     validation             3/3            2/3
#     serialization          2/3            0/3
#     events                 3/3            1/3
#     capacity               3/3            1/3
#     a query method         3/3            1/3
#     doc comments           0/3            0/3
#
# It did not help documentation and it cost everything else. Reverted.
# Whatever governs which rule survives, it is not simply distance from
# the end, and one more rearrangement would be guessing with the user's
# output as the test bed.
#
# The rules that are Unity's and belong to no single global property.
#
# The first three exist because they were MEASURED missing. Across six
# runs of an inventory prompt, doc comments appeared 0 times, events 2,
# and a query method 0 -- so each is now asked for by name rather than
# implied by "document it" and "expose hooks".
RULES = (
    "Every public member gets a /// <summary> comment. The inspector and "
    "IntelliSense both read it, and a public member without one is not "
    "finished.",
    "Use Debug.LogWarning for a recoverable problem and Debug.LogError "
    "for a broken one, rather than throwing, so play mode continues and "
    "the message lands in the console.",
    "Never assume a serialized reference is set: null-check it and log "
    "which field is missing.",
    "[CreateAssetMenu] on any ScriptableObject a designer should be able "
    "to create from the Assets menu.",
    "GetComponentInChildren and GetComponentInParent when the component "
    "may be on a relative rather than on this object.",
)

# What each KIND of system needs, delivered only when the request is
# about that kind. Unity knows far more than fits in one turn's window,
# and paying for the dialogue rules on an inventory prompt costs the
# model the attention it needs for the file.
TOPICS = (
    BriefTopic(
        name="containers",
        triggers=("inventory", "container", "backpack", "storage", "bag",
                  "hotbar", "toolbar", "slots", "stash", "chest", "loadout"),
        rules=(
            "A container needs Add, Remove AND a query -- HasItem or "
            "Contains -- plus a Count or an enumerator. A container you "
            "cannot ask about is not finished.",
            "Stackable items: a max stack size, and Add that fills an "
            "existing stack before taking a new slot.",
            "Capacity: a serialized maximum, a check before every Add, "
            "and Debug.LogWarning when it is full or when a Remove would "
            "go below zero. Never let either wrap silently.",
            "Raise an event on add and on remove, both, so a UI can "
            "listen rather than poll.",
        ),
    ),
    BriefTopic(
        name="dialogue",
        triggers=("dialogue", "dialog", "conversation", "barks", "cutscene"),
        rules=(
            "Nodes as [Serializable] data, branching by id rather than by "
            "object reference, conditions and triggers as small "
            "interfaces, and the whole graph saveable.",
            "A ScriptableObject per conversation so writers can edit it "
            "without touching a scene.",
        ),
    ),
    BriefTopic(
        name="crafting",
        triggers=("crafting", "recipe", "recipes", "smelting", "brewing"),
        rules=(
            "Recipes as ScriptableObjects: inputs, outputs, and a "
            "CanCraft that answers without consuming anything.",
            "Craft consumes only after CanCraft passes, and raises an "
            "event either way.",
        ),
    ),
    BriefTopic(
        name="ai",
        triggers=("ai", "enemy", "npc", "behaviour", "behavior", "patrol",
                  "state machine", "pathfinding"),
        rules=(
            "A state enum and one method per state, driven from Update or "
            "a coroutine -- not a chain of bools.",
            "Perception through overlap queries or triggers, with the "
            "radius and mask as [SerializeField], and OnDrawGizmosSelected "
            "so it can be seen in the scene view.",
        ),
    ),
    BriefTopic(
        name="ui",
        triggers=("ui", "hud", "menu", "canvas", "button", "panel", "widget"),
        rules=(
            "A UI controller listens to the system's events and never "
            "polls it in Update.",
            "Serialized references to the widgets it drives, null-checked "
            "in Awake with a Debug.LogError naming the missing field.",
        ),
    ),
    BriefTopic(
        name="persistence",
        triggers=("save", "load", "persistence", "savegame", "checkpoint",
                  "serialization", "serialize"),
        rules=(
            "A [Serializable] save DTO separate from the runtime type, so "
            "the save format can change without breaking gameplay code.",
            "JsonUtility to and from Application.persistentDataPath, with "
            "the read wrapped in a try/catch that logs and returns a fresh "
            "default rather than throwing on a corrupt file.",
            "A version field in the save data from the first version "
            "onward.",
        ),
    ),
    BriefTopic(
        name="events",
        triggers=("event bus", "eventbus", "messaging", "pubsub",
                  "observer", "signals"),
        rules=(
            "A ScriptableObject event channel that both raiser and "
            "listener reference as an asset, so neither needs to find the "
            "other in the scene.",
            "Unsubscribe in OnDisable everything subscribed in OnEnable.",
        ),
    ),
    BriefTopic(
        name="editor",
        triggers=("editor", "inspector", "gizmo", "gizmos", "editorwindow",
                  "custom inspector", "assetdatabase", "build pipeline",
                  "addressable", "addressables"),
        rules=(
            "Editor code lives under an Editor/ folder or inside "
            "#if UNITY_EDITOR, so it never ships in a build.",
            "CustomEditor with SerializedObject and SerializedProperty "
            "rather than editing the target directly, so undo and "
            "multi-object editing keep working.",
            "AssetDatabase.CreateAsset then SaveAssets; refresh once at "
            "the end rather than per asset.",
        ),
    ),
)

PLUGIN = BriefPlugin(
    name="Unity C#",
    idioms=IDIOMS,
    serialization=SERIALIZATION,
    events=EVENTS,
    tests=TESTS,
    rules=RULES,
    topics=TOPICS,
    applies_to=INTENT_HINTS,
)


def register() -> None:
    """Make this plugin available to the brief."""
    register_plugin(PLUGIN)
