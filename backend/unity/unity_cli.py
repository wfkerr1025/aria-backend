# =========================================================
# unity_cli.py — Unity Command Handler (Unified Architecture)
# =========================================================

import json
import os

from aria_theme import TAG_UNITY, TAG_ERROR, TAG_INFO, TAG_FOUND

from backend.unity.unity_ops import (
    find_unity_projects,
    summarize_unity_project,
    unity_create_script,
    unity_create_scene,
    unity_create_prefab,
    unity_create_scriptableobject,
)

ACTIVE_UNITY_PROJECT_FILE = ".aria_active_unity_project.json"


# =========================================================
# ACTIVE PROJECT STORAGE
# =========================================================
def save_active_unity_project(project):
    try:
        with open(ACTIVE_UNITY_PROJECT_FILE, "w", encoding="utf-8") as f:
            json.dump(project, f, indent=2)
    except Exception:
        print(f"{TAG_ERROR} Failed to save active Unity project.")


def load_active_unity_project():
    if not os.path.exists(ACTIVE_UNITY_PROJECT_FILE):
        return None
    try:
        with open(ACTIVE_UNITY_PROJECT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        print(f"{TAG_ERROR} Failed to load active Unity project.")
        return None


# =========================================================
# MAIN COMMAND HANDLER
# =========================================================
def handle_unity(args, config):
    """
    Unified Unity CLI handler.
    Commands:
      - list
      - select
      - scenes
      - assets
      - build
      - create <script|scene|prefab|so> <name>
    """

    if not args:
        print(f"{TAG_UNITY} Commands: list, select, scenes, assets, build, create")
        return

    sub = args[0].lower()
    unity_cfg = config.get("unity", {})
    search_entire_pc = unity_cfg.get("search_entire_pc", True)

    active = load_active_unity_project()

    # ---------------------------------------------------------
    # LIST PROJECTS
    # ---------------------------------------------------------
    if sub == "list":
        projects = find_unity_projects(search_entire_pc=search_entire_pc)
        for p in projects:
            print(f"{TAG_FOUND} {p['name']} ({p['path']})")
        return

    # ---------------------------------------------------------
    # SELECT PROJECT
    # ---------------------------------------------------------
    if sub == "select":
        projects = find_unity_projects(search_entire_pc=search_entire_pc)

        if not projects:
            print(f"{TAG_ERROR} No Unity projects found.")
            return

        for i, p in enumerate(projects):
            print(f"[{i}] {p['name']} ({p['path']})")

        try:
            choice = int(input("Select project index: "))
            project = projects[choice]
            save_active_unity_project(project)
            print(f"{TAG_UNITY} Active project set to {project['name']}")
        except Exception:
            print(f"{TAG_ERROR} Invalid selection.")
        return

    # ---------------------------------------------------------
    # REQUIRE ACTIVE PROJECT
    # ---------------------------------------------------------
    if not active:
        print(f"{TAG_ERROR} No active project. Run: aria unity select")
        return

    project_path = active["path"]

    # ---------------------------------------------------------
    # SCENES
    # ---------------------------------------------------------
    if sub == "scenes":
        summary = summarize_unity_project(project_path)
        for s in summary["scenes"]:
            print(f"{TAG_INFO} {s['name']} ({s['path']})")
        return

    # ---------------------------------------------------------
    # ASSETS
    # ---------------------------------------------------------
    if sub == "assets":
        summary = summarize_unity_project(project_path)
        for kind, items in summary["assets"].items():
            print(f"{TAG_INFO} [{kind}]")
            for a in items[:20]:
                print(f" - {a['name']}")
        return

    # ---------------------------------------------------------
    # BUILD SETTINGS
    # ---------------------------------------------------------
    if sub == "build":
        summary = summarize_unity_project(project_path)
        for b in summary["build_scenes"]:
            print(f"{TAG_INFO} {b['path']} [{b['enabled']}]")
        return

    # ---------------------------------------------------------
    # CREATE OPERATIONS
    # ---------------------------------------------------------
    if sub == "create":
        if len(args) < 3:
            print(f"{TAG_ERROR} Usage: aria unity create <script|scene|prefab|so> <name>")
            return

        create_type = args[1].lower()
        name = args[2]

        if create_type == "script":
            unity_create_script(project_path, name)
        elif create_type == "scene":
            unity_create_scene(project_path, name)
        elif create_type == "prefab":
            unity_create_prefab(project_path, name)
        elif create_type == "so":
            unity_create_scriptableobject(project_path, name)
        else:
            print(f"{TAG_ERROR} Unknown create type.")
        return

    # ---------------------------------------------------------
    # UNKNOWN COMMAND
    # ---------------------------------------------------------
    print(f"{TAG_ERROR} Unknown command.")
