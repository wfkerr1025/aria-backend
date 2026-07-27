from pathlib import Path

EDITOR_BUILD_SETTINGS_FILE = "ProjectSettings/EditorBuildSettings.asset"


def read_build_settings(project_path: str):
    """
    Read Unity EditorBuildSettings.asset and extract scene entries.
    Returns a list of:
        { "path": <scene path>, "enabled": <bool> }
    """

    build_file = Path(project_path) / EDITOR_BUILD_SETTINGS_FILE
    if not build_file.exists():
        return []

    scenes = []
    text = build_file.read_text(encoding="utf-8", errors="ignore")

    current_path = None
    current_enabled = True

    for line in text.splitlines():
        stripped = line.strip()

        # Scene path
        if stripped.startswith("path:"):
            current_path = stripped.split("path:", 1)[-1].strip()

        # Enabled flag (Unity uses "enabled: 1" or "enabled: 0")
        if stripped.startswith("enabled:"):
            val = stripped.split("enabled:", 1)[-1].strip()
            current_enabled = val in ("1", "true", "True")

        # When both are found, commit the scene
        if current_path is not None:
            scenes.append({
                "path": current_path,
                "enabled": current_enabled
            })
            current_path = None
            current_enabled = True

    return scenes
