from __future__ import annotations
from typing import Dict, Any, List, Tuple
import re
import difflib

from backend.llm.natural_language_map import natural_language_map


class IntentEngine:
    """
    A semantic intent engine that interprets natural language and produces
    tool envelopes for ARIA Lite. This engine bridges the gap between
    user phrasing → intent category → tool envelope.
    """

    ###########################################################################
    # MAIN ENTRY POINT
    ###########################################################################
    def interpret(self, text: str) -> Dict[str, Any]:
        """
        Interpret natural language and return a tool envelope.
        If no tool intent is detected, return an assistant-mode envelope.
        """

        text_lower = text.lower().strip()

        # 1. Extract verbs and nouns from the text
        verbs = self._extract_verbs(text_lower)
        nouns = self._extract_nouns(text_lower)

        # 2. Score all categories
        scores = self._score_categories(text_lower, verbs, nouns)

        # 3. Pick the best category
        best_category, best_score = self._pick_best_category(scores)

        if best_score < 0.25:
            # Not confident enough → assistant mode
            return {
                "type": "assistant",
                "content": text
            }

        # 4. Build tool envelope
        envelope = self._build_tool_envelope(best_category, text_lower)

        return envelope


    ###########################################################################
    # VERB / NOUN EXTRACTION
    ###########################################################################
    def _extract_verbs(self, text: str) -> List[str]:
        """
        Extract verbs by matching against all verbs in the NL map.
        """
        all_verbs = []
        for category in natural_language_map.values():
            all_verbs.extend(category.get("verbs", []))

        found = []
        for verb in all_verbs:
            if verb in text:
                found.append(verb)

        return found


    def _extract_nouns(self, text: str) -> List[str]:
        all_nouns = []
        for category in natural_language_map.values():
            for noun in category.get("nouns", []):
                all_nouns.append(noun.strip().lower())

        found = []
        for noun in all_nouns:
            if noun and noun in text:
                found.append(noun)

        return found

    ###########################################################################
    # CATEGORY SCORING
    ###########################################################################
    def _score_categories(self, text: str, verbs: List[str], nouns: List[str]) -> Dict[str, float]:
        """
        Score each category based on:
        - verb matches
        - noun matches
        - pattern matches
        - fuzzy similarity
        """

        scores = {}

        for category_name, category in natural_language_map.items():
            score = 0.0

            # Verb matches
            for verb in category.get("verbs", []):
                if verb in text:
                    score += 0.25

            # Noun matches
            for noun in category.get("nouns", []):
                if noun in text:
                    score += 0.25

            # Pattern matches
            for pattern in category.get("patterns", []):
                pattern_clean = pattern.replace("{X}", "").strip()
                if pattern_clean and pattern_clean in text:
                    score += 0.35

            # Fuzzy similarity
            for pattern in category.get("patterns", []):
                sim = difflib.SequenceMatcher(None, pattern.lower(), text.lower()).ratio()
                if sim > 0.65:
                    score += 0.15

            scores[category_name] = score

        return scores


    ###########################################################################
    # PICK BEST CATEGORY
    ###########################################################################
    def _pick_best_category(self, scores: Dict[str, float]) -> Tuple[str, float]:
        """
        Return the category with the highest score.
        """
        best_category = max(scores, key=scores.get)
        best_score = scores[best_category]
        return best_category, best_score


    ###########################################################################
    # TOOL ENVELOPE BUILDER
    ###########################################################################
    def _build_tool_envelope(self, category: str, text: str) -> Dict[str, Any]:
        """
        Build the correct tool envelope based on the category.
        """

        info = natural_language_map[category]
        tool = info.get("tool")

        # Directory creation → extract directory name
        if category == "directory_creation":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "mkdir",
                "path": path
            }

        # File read
        if category == "file_read":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "read",
                "path": path
            }

        # File write
        if category == "file_write":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "write",
                "path": path,
                "content": self._extract_content(text)
            }

        # File delete
        if category == "file_delete":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "delete",
                "path": path
            }

        # Patching
        if category == "patching":
            path = self._extract_path(text)
            diff = self._extract_diff(text)
            return {
                "task": tool,
                "path": path,
                "diff": diff
            }

        # Copy / move / rename
        if category == "file_copy_move":
            src, dst = self._extract_src_dst(text)
            return {
                "task": tool,
                "src": src,
                "dst": dst,
                "operation": self._infer_copy_move_operation(text)
            }

        # Metadata
        if category == "metadata":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "info",
                "path": path
            }

        # Security
        if category == "security":
            path = self._extract_path(text)
            return {
                "task": tool,
                "operation": "validate",
                "path": path
            }

        # Testing
        if category == "testing":
            return {
                "task": tool
            }

        # Workspace management → context engine
        if category == "workspace_management":
            return {
                "task": tool,
                "data": {
                    "text": text,
                    "mode": "all"
                }
            }

        # Fallback → assistant mode
        return {
            "type": "assistant",
            "content": text
        }


    ###########################################################################
    # EXTRACTION HELPERS
    ###########################################################################
    def _extract_path(self, text: str) -> str:
        """
        Extract a file or directory name from the text.
        """
        tokens = re.findall(r"[a-zA-Z0-9_\-\.]+", text)
        if len(tokens) > 1:
            return tokens[-1]
        return tokens[0] if tokens else "unknown"


    def _extract_content(self, text: str) -> str:
        """
        Extract content for write operations.
        """
        if "with" in text:
            return text.split("with", 1)[1].strip()
        if "content" in text:
            return text.split("content", 1)[1].strip()
        return ""


    def _extract_diff(self, text: str) -> str:
        """
        Extract diff content for patch operations.
        """
        if "diff" in text:
            return text.split("diff", 1)[1].strip()
        return ""


    def _extract_src_dst(self, text: str) -> Tuple[str, str]:
        """
        Extract source and destination for copy/move operations.
        """
        parts = re.findall(r"[a-zA-Z0-9_\-\.]+", text)
        if len(parts) >= 2:
            return parts[-2], parts[-1]
        return "unknown", "unknown"


    def _infer_copy_move_operation(self, text: str) -> str:
        """
        Infer whether the user wants copy, move, or rename.
        """
        if "copy" in text:
            return "copy"
        if "move" in text:
            return "move"
        if "rename" in text:
            return "rename"
        return "copy"  # default fallback


# Singleton instance
intent_engine = IntentEngine()
