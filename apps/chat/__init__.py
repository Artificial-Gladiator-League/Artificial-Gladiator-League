"""Minimal chat app package for notifications (lightweight placeholder).

This module intentionally left minimal — the project previously removed
the full chat app; we add a small, self-contained notifications consumer
and HTTP endpoints so the frontend and signals don't fail when trying
to open /ws/notifications/ or call /chat/notifications/.
"""

__all__ = ["consumers", "views", "routing", "urls"]
