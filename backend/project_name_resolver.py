def resolve_project_name_from_context(context_snapshot):
    """
    Unified project name resolver for ARIA Lite.
    Reads topics/keywords from ContextEngine snapshots and maps them
    to project names. Returns a structured result dictionary.
    """

    # ContextEngine snapshot structure:
    # {
    #   "context": {
    #       "entities": { entity_id: { ... } },
    #       ...
    #   },
    #   "turn": <int>
    # }

    context = context_snapshot.get("context")
    if not context:
        return {
            "status": "ok",
            "operation": "resolve_project_name",
            "project": "General",
            "detail": "No active context"
        }

    entities = context.get("entities", {})
    topics = []

    # Extract topics from multiple entity types
    for ent_id, ent_data in entities.items():

        # Explicit topic entity
        if ent_data.get("type") == "topic":
            topics.append(ent_data.get("topic"))

        # ARIAKeyword → treat as topic
        if ent_data.get("type") == "ARIAKeyword":
            topics.append(ent_data.get("value"))

        # Named entities (e.g., Unity, Blender)
        if ent_data.get("type") == "Name":
            topics.append(ent_data.get("value").lower())

    # Normalize
    topics = [t.lower() for t in topics]

    # Mapping
    if "aria" in topics:
        project = "ARIA Lite"
    elif "unity" in topics:
        project = "Unity Project"
    elif "blender" in topics:
        project = "Blender Project"
    elif "unreal" in topics:
        project = "Unreal Project"
    elif "wordpress" in topics:
        project = "WordPress Site"
    else:
        project = "General"

    return {
        "status": "ok",
        "operation": "resolve_project_name",
        "project": project,
        "topics": topics
    }
