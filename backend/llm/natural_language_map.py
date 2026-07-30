natural_language_map = {

    ###########################################################################
    # DIRECTORY CREATION / WORKSPACE SETUP
    ###########################################################################
    "directory_creation": {
        "verbs": [
            "create", "make", "build", "prepare", "set up", "spin up",
            "initialize", "generate", "construct", "establish", "form",
            "produce", "start", "start up", "get ready", "get", "ensure"
        ],
        "nouns": [
            "directory", "folder", "workspace", "area", "location",
            "place", "environment", "shadow", "workspace_shadow"
        ],
        "patterns": [
            "create {X}", "create a {X}", "create the {X}",
            "create a {X} directory", "create the {X} directory",
            "make {X}", "make the {X}", "make a {X} folder",
            "prepare {X}", "prepare the {X}", "prepare the {X} directory",
            "set up {X}", "set up the {X}", "spin up {X}",
            "initialize {X}", "initialize the {X} directory",
            "get {X} ready", "get the {X} directory ready",
            "ensure {X} exists", "ensure the {X} directory exists",
            "I need a {X} directory", "I need the {X} folder",
            "can you create {X}", "can you make {X}",
            "please create {X}", "please make {X}",
            "build the {X} directory", "start the {X} workspace"
        ],
        "tool": "file_ops",
        "operation": "mkdir"
    },

    ###########################################################################
    # FILE READ
    ###########################################################################
    "file_read": {
        "verbs": [
            "read", "open", "show", "display", "view", "inspect",
            "look at", "load", "fetch"
        ],
        "patterns": [
            "read {X}", "open {X}", "show me {X}", "display {X}",
            "view {X}", "inspect {X}", "look at {X}", "load {X}",
            "fetch {X}", "what's in {X}", "what is inside {X}"
        ],
        "tool": "file_ops",
        "operation": "read"
    },

    ###########################################################################
    # FILE WRITE
    ###########################################################################
    "file_write": {
        "verbs": [
            "write", "save", "update", "append", "overwrite",
            "store", "record", "put", "place", "insert"
        ],
        "patterns": [
            "write to {X}", "write this to {X}", "save this to {X}",
            "update {X}", "append to {X}", "overwrite {X}",
            "store this in {X}", "put this in {X}", "insert this into {X}",
            "replace contents of {X}", "update the file {X}"
        ],
        "tool": "file_ops",
        "operation": "write"
    },

    ###########################################################################
    # FILE DELETE
    ###########################################################################
    "file_delete": {
        "verbs": [
            "delete", "remove", "erase", "clear", "wipe", "discard"
        ],
        "patterns": [
            "delete {X}", "remove {X}", "erase {X}", "clear {X}",
            "wipe {X}", "discard {X}", "get rid of {X}"
        ],
        "tool": "file_ops",
        "operation": "delete"
    },

    ###########################################################################
    # PATCHING / CODE FIXES
    ###########################################################################
    "patching": {
        "verbs": [
            "patch", "fix", "correct", "apply", "modify", "change",
            "update", "repair", "resolve", "address"
        ],
        "patterns": [
            "patch {X}", "fix {X}", "correct {X}", "apply this to {X}",
            "modify {X}", "change {X}", "update {X} with this",
            "repair {X}", "resolve issue in {X}", "address problem in {X}"
        ],
        "tool": "patch"
    },

    ###########################################################################
    # COPY / MOVE / RENAME
    ###########################################################################
    "file_copy_move": {
        "verbs": [
            "copy", "move", "transfer", "shift", "duplicate",
            "clone", "relocate", "rename"
        ],
        "patterns": [
            "copy {X} to {Y}", "move {X} to {Y}", "transfer {X} to {Y}",
            "shift {X} to {Y}", "duplicate {X}", "clone {X}",
            "relocate {X}", "rename {X} to {Y}"
        ],
        "tool": "fs"
    },

    ###########################################################################
    # METADATA / FILE INFO
    ###########################################################################
    "metadata": {
        "verbs": [
            "check", "verify", "inspect", "look up", "find",
            "determine", "see", "confirm"
        ],
        "patterns": [
            "does {X} exist", "does {X} exist?", "check if {X} exists",
            "verify {X}", "inspect metadata for {X}", "show metadata for {X}",
            "list files in {X}", "hash {X}", "fingerprint {X}"
        ],
        "tool": "metadata"
    },

    ###########################################################################
    # SECURITY / VALIDATION
    ###########################################################################
    "security": {
        "verbs": [
            "validate", "check", "verify", "ensure", "confirm"
        ],
        "patterns": [
            "validate this path {X}", "check workspace boundary for {X}",
            "is {X} in the workspace", "ensure {X} is safe"
        ],
        "tool": "security"
    },

    ###########################################################################
    # TESTING
    ###########################################################################
    "testing": {
        "verbs": [
            "test", "run", "validate", "check", "execute"
        ],
        "patterns": [
            "run tests", "test the workspace", "sandbox this",
            "validate everything works", "execute tests"
        ],
        "tool": "run_python_tests"
    },

    ###########################################################################
    # WORKSPACE MANAGEMENT
    ###########################################################################
    "workspace_management": {
        "verbs": [
            "prepare", "set up", "initialize", "reset", "clean",
            "refresh", "rebuild", "configure"
        ],
        "patterns": [
            "prepare the workspace", "set up the workspace",
            "initialize the workspace", "reset the workspace",
            "clean the workspace", "refresh the workspace",
            "rebuild the workspace", "configure the workspace"
        ],
        "tool": "context"
    },

    ###########################################################################
    # GENERAL INTENT (fallback)
    ###########################################################################
    "general_intent": {
        "verbs": [
            "do", "perform", "handle", "manage", "take care of",
            "deal with", "work on", "process"
        ],
        "patterns": [
            "handle this", "take care of this", "deal with this",
            "work on this", "process this"
        ],
        "tool": "llm"
    }
}
