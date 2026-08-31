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

from dataclasses import dataclass, field

from logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "BriefPlugin",
    "active_plugin_sections",
    "clear_plugins",
    "register_plugin",
    "registered_plugins",
]

# A plugin's whole contribution, per turn. Generous for a paragraph of
# real conventions and far too small for a pasted document.
MAX_PLUGIN_CHARS = 1200


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

    def section(self) -> str:
        """This plugin's lines, or "" when it has nothing to say."""
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
        return any(str(suffix).lower() in lowered for suffix in self.applies_to)


_PLUGINS: dict = {}


def register_plugin(plugin: BriefPlugin) -> None:
    """Add a domain's conventions. Re-registering a name replaces it."""
    if not isinstance(plugin, BriefPlugin) or not plugin.name:
        raise ValueError("a brief plugin needs a name")
    _PLUGINS[plugin.name] = plugin
    logger.info("registered brief plugin %r", plugin.name)


def clear_plugins() -> None:
    """Forget every plugin. For tests, and for a workspace change."""
    _PLUGINS.clear()


def registered_plugins() -> list:
    return [_PLUGINS[name] for name in sorted(_PLUGINS)]


def active_plugin_sections(hint: str = "") -> list:
    """The sections that apply, in a stable order.

    Never raises. A plugin that throws while describing itself costs its
    own section and not the turn -- the global brief is the part that
    must always arrive.
    """
    sections = []
    for plugin in registered_plugins():
        try:
            if not plugin.wants(hint):
                continue
            section = plugin.section()
            if section:
                sections.append(section)
        except Exception:  # pragma: no cover - a plugin is not worth a turn
            logger.exception("brief plugin %r failed; skipping its section",
                             getattr(plugin, "name", "?"))
    return sections
