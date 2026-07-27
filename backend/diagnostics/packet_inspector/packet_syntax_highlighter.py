# packet_syntax_highlighter.py
import json


def highlight_json(text_widget):
    """
    Highlights JSON validity inside a Tk Text widget.
    - Removes error tag if JSON is valid
    - Applies error tag to entire content if invalid
    """

    raw = text_widget.get("1.0", "end").strip()

    # Configure tag once
    text_widget.tag_configure("error", foreground="red")

    try:
        json.loads(raw)
        # JSON is valid → remove error highlight
        text_widget.tag_remove("error", "1.0", "end")
    except Exception:
        # JSON invalid → highlight entire content
        text_widget.tag_add("error", "1.0", "end")
