"""ARIA Lite Phase 4 - file subsystem.

Ingestion turns a file on disk into embedded, searchable chunks;
file_search puts those chunks through the same ranking the note store uses,
so a file chunk and a note compete on the same terms.
"""
