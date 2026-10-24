"""Re-export so the Java-style layout (NodeRecord next to GameTrace) is preserved."""
from apps.shiri.database.game_trace import NodeRecord

__all__ = ["NodeRecord"]
