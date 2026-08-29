"""ARIA Lite - turn-level chat architecture: which model, and checking its output.

backend/chat/ holds the decisions made ABOUT a turn rather than the
machinery that runs one. model_router picks the model before anything
generates; supervisor_layer checks what came back. Both are pure -- they
return values and perform no I/O -- so the orchestrator stays the only
place that acts.
"""
