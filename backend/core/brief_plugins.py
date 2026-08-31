"""ARIA Lite - where a domain says what its systems look like.

The global brief says HOW ARIA builds: completely, with validation, with
persistence, with hooks, documented, tested. It deliberately says nothing
about WHAT any of those mean in a particular world, because the moment it
does it is wrong everywhere else. "Serialize with JsonUtility" is right
in Unity and meaningless in Django; "expose UnityEvents" is right in one
engine and nonsense in the next.

So the global brief asks for a property and a plugin names the mechanism.
Save and load becomes JsonUtility here and pickle there; hooks become
C# events here and signals there; tests follow whatever this project
actually uses. ARIA with no plugins still writes complete systems -- it
just writes them in the plain idiom of the language, which is the right
default and is what the ninth requirement means by "even before plugins
are added".

WHAT A PLUGIN MAY NOT DO
------------------------
It contributes text to a brief. It cannot add a tool, widen a
permission, grant a consent, or reach the executor: the brief is a
system message and nothing downstream reads it as authority. That is not
a restriction this module enforces by cleverness -- it is simply the
only thing a brief IS, and keeping plugins on this side of that line is
why they live here rather than in tool_registry.

Plugin text is also bounded. A plugin that pasted a style guide into
every turn would spend the window the model needs for the file it is
being asked to write, and the failure would look like a worse model
rather than a longer prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "BriefPlugin",
    "BriefTopic",
    "active_plugin_sections",
    "clear_plugins",
    "load_builtins",
    "register_plugin",
    "registered_plugins",
]

# A word is a word; an extension is not.
#
# Plain substring matching was wrong in both directions and one of them
# was embarrassing: a plugin scoped to "unity" fired on "I see an
# opportunity to refactor" and on "the community wants this". A user
# discussing an opportunity would have had Unity idioms pushed at them.
#
# So a token made only of word characters is matched as a WORD, and a
# token with punctuation in it -- ".cs", "c#" -- is matched as a
# substring, because those never sit on a word boundary the regex would
# recognise and they are specific enough not to need one.
_WORDLIKE = re.compile(r"^\w+$")


def _token_in(haystack: str, token: str) -> bool:
    if not token:
        return False
    if _WORDLIKE.match(token):
        return re.search(rf"\b{re.escape(token)}\b", haystack) is not None
    return token in haystack


# A plugin's whole contribution, per turn.
#
# Raised from 1200 when topics arrived: the Unity plugin's always-true
# conventions alone are about 1150 characters, so a topic's rules were
# being silently truncated off the end -- the guidance most specific to
# the request was the guidance being cut.
#
# Still bounded, and the bound still matters. This is about 700 tokens
# on top of the global brief; a plugin that fills it is spending the
# window the model needs for the file, and the failure looks like a
# worse model rather than a longer prompt.
MAX_PLUGIN_CHARS = 2400


@dataclass(frozen=True)
class BriefTopic:
    """Guidance for one kind of system, carried by a plugin.

    A domain knows more than fits in a turn. Topics are how it says the
    part that matters for THIS request and stays quiet about the rest:
    an inventory prompt should not pay for the dialogue rules.
    """

    name: str
    triggers: tuple = field(default_factory=tuple)
    rules: tuple = field(default_factory=tuple)

    def wants(self, hint: str) -> bool:
        lowered = str(hint or "").lower()
        return any(_token_in(lowered, str(trigger).lower())
                   for trigger in self.triggers)


@dataclass(frozen=True)
class BriefPlugin:
    """What one domain adds to the global brief.

    Every field is optional. A plugin that only knows how its world
    serializes fills in one line and inherits the rest.
    """

    name: str
    # "Unity MonoBehaviour components, one responsibility each."
    idioms: str = ""
    # "JsonUtility.ToJson / FromJson, into Application.persistentDataPath."
    serialization: str = ""
    # "UnityEvent fields, so designers can wire them in the inspector."
    events: str = ""
    # "NUnit, under Assets/Tests, one file per system."
    tests: str = ""
    # Anything else this domain requires, as whole sentences.
    rules: tuple = field(default_factory=tuple)
    # Which files this applies to, by extension. Empty means every file.
    applies_to: tuple = field(default_factory=tuple)
    # Guidance for one KIND of system, included only when the request is
    # about that kind. See section().
    topics: tuple = field(default_factory=tuple)

    def section(self, hint: str = "") -> str:
        """This plugin's lines, or "" when it has nothing to say.

        `hint` selects TOPICS. A domain knows far more than fits in one
        turn's window -- Unity alone has inventories, dialogue, crafting,
        AI, UI, saving, event buses -- and pasting all of it costs the
        model the attention it needs for the file. Measured: adding
        conventions moved output toward idiom and away from
        completeness, and that was one page, not five.

        So the always-true conventions are the plugin's own fields, and
        anything that only applies to one KIND of system is a topic that
        arrives when the request is about it.
        """
        lines = []
        if self.idioms:
            lines.append(f"- Architecture: {self.idioms}")
        if self.serialization:
            lines.append(f"- Saving and loading: {self.serialization}")
        if self.events:
            lines.append(f"- Hooks: {self.events}")
        if self.tests:
            lines.append(f"- Tests: {self.tests}")
        lines.extend(f"- {rule}" for rule in self.rules if str(rule).strip())

        for topic in self.topics:
            if topic.wants(hint):
                lines.extend(f"- {rule}" for rule in topic.rules if str(rule).strip())

        if not lines:
            return ""

        body = "\n".join(lines)
        if len(body) > MAX_PLUGIN_CHARS:
            logger.warning("plugin %s contributes %d chars; truncating to %d",
                           self.name, len(body), MAX_PLUGIN_CHARS)
            body = body[:MAX_PLUGIN_CHARS].rsplit("\n", 1)[0]
        return f"In this project ({self.name}):\n{body}"

    def wants(self, hint: str) -> bool:
        """Whether this plugin has anything to say about this turn.

        Matched against a HINT, not a path, and the difference was found
        by testing it: the brief is built before the model has proposed
        anything, so there is no path yet. A plugin scoped by file
        extension could therefore never apply, and a Unity plugin
        registered against ".cs" contributed nothing to a turn that went
        on to write a .cs file.

        The hint is whatever the turn knows about itself -- the user's
        words, and a path once there is one. "create a player_inventory.cs
        file" carries ".cs" in it, which is exactly the signal a
        language-scoped plugin needs and the only one available this
        early.

        Substring, not suffix, for the same reason: ".cs" appears inside
        a sentence, never at the end of one.
        """
        if not self.applies_to:
            return True

        lowered = str(hint or "").lower()
        return any(_token_in(lowered, str(token).lower())
                   for token in self.applies_to)


_PLUGINS: dict = {}


def register_plugin(plugin: BriefPlugin) -> None:
    """Add a domain's conventions. Re-registering a name replaces it."""
    if not isinstance(plugin, BriefPlugin) or not plugin.name:
        raise ValueError("a brief plugin needs a name")
    _PLUGINS[plugin.name] = plugin
    logger.info("registered brief plugin %r", plugin.name)


# Whether the built-in plugins have been dealt with this process.
#
# Set by loading them AND by clearing them, and the second half matters:
# without it a test that cleared the registry would get Unity's section
# back on the next brief, and every test of the GLOBAL brief would
# quietly be testing something else.
_BUILTINS_HANDLED = False


def _ensure_builtins() -> None:
    """Register the plugins that ship with ARIA, once."""
    global _BUILTINS_HANDLED
    if _BUILTINS_HANDLED:
        return
    _BUILTINS_HANDLED = True
    try:
        from backend import plugins

        plugins.load_builtins()
    except Exception:  # pragma: no cover - a plugin is not worth a turn
        logger.exception("could not load the built-in brief plugins")


def load_builtins(force: bool = False) -> None:
    """Load the shipped plugins. `force` re-loads after a clear."""
    global _BUILTINS_HANDLED
    if force:
        _BUILTINS_HANDLED = False
    _ensure_builtins()


def clear_plugins() -> None:
    """Forget every plugin, and do not quietly reload the built-in ones.

    For tests, and for a workspace change.
    """
    global _BUILTINS_HANDLED
    _PLUGINS.clear()
    _BUILTINS_HANDLED = True


def registered_plugins() -> list:
    return [_PLUGINS[name] for name in sorted(_PLUGINS)]


def active_plugin_sections(hint: str = "") -> list:
    """The sections that apply, in a stable order.

    Never raises. A plugin that throws while describing itself costs its
    own section and not the turn -- the global brief is the part that
    must always arrive.
    """
    _ensure_builtins()

    sections = []
    for plugin in registered_plugins():
        try:
            if not plugin.wants(hint):
                continue
            section = plugin.section(hint)
            if section:
                sections.append(section)
        except Exception:  # pragma: no cover - a plugin is not worth a turn
            logger.exception("brief plugin %r failed; skipping its section",
                             getattr(plugin, "name", "?"))
    return sections
