"""Utilities for normalizing titles."""


def normalize_title(text: str) -> str:
    """Collapse Unicode whitespace and trim; raise ValueError for a blank title."""
    title = " ".join(text.split())
    if not title:
        raise ValueError("Title must not be empty or whitespace-only")
    return title
