"""TreeNode shared by UCT and MCVS.

``w`` accumulates reward from the point of view of ``mover`` (the player whose move led
to this node), so a parent picks the child with the best ``w / n`` for itself.
"""
from __future__ import annotations

from typing import Optional


class TreeNode:
    __slots__ = ("parent", "move", "state", "mover", "children", "untried",
                 "prior", "zone", "zraw", "n", "w", "result", "expanded")

    def __init__(self, parent: Optional["TreeNode"], move, state, mover: int,
                 prior: float = 1.0, zone: Optional[float] = None, result: Optional[int] = None,
                 zraw: float = 0.0):
        self.parent = parent
        self.move = move
        self.state = state
        self.mover = mover
        self.children: list[TreeNode] = []
        self.untried: Optional[list] = None   # UCT only: moves not yet turned into children
        self.prior = prior
        self.zone = zone                      # relative zone score in [-1, 1] (MCVS only)
        self.zraw = zraw                      # absolute zone score from ``mover``'s view (MCVS only)
        self.n = 0
        self.w = 0.0
        self.result = result
        self.expanded = False                 # MCVS only: all children created

    def q(self, default: float = 0.5) -> float:
        return self.w / self.n if self.n else default

    def most_visited(self) -> Optional["TreeNode"]:
        if not self.children:
            return None
        return max(self.children, key=lambda c: (c.n, c.q()))
