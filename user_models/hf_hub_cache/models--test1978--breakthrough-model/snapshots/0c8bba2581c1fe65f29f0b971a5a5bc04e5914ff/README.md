---
license: mit
pretty_name: Breakthrough MCVS - Zone Guided AI
tags:
  - breakthrough
  - game-ai
  - monte-carlo-tree-search
  - reinforcement-learning
  - zone-guidance
  - adjacency-matrix
  - hilbert-curve
  - abc-model
  - pytorch
  - numpy
task_categories:
  - other
---

# Breakthrough MCVS - Zone Guided AI

**Advanced Monte-Carlo Value Search (MCVS)** engine for the game **Breakthrough** (8x8), powered by a displacement-based **ABC Model** and **Weighted Adjacency Matrices** with **Hilbert-ordered Zone Guidance**.

This implementation adapts the zone-guided MCVS framework to the simple but illustrative game Breakthrough, keeping the same neural architectures and zone-database design used by the chess reference implementation.

## Core Idea

The engine uses:
- Displacement-based ABC Model with homogeneous coordinates to represent piece displacements succinctly
- Dynamic Weighted Adjacency Matrices `W = A ⊙ S ⊙ F` representing spatial, adjacency and feature similarity
- Hilbert curve ordering for efficient neighborhood (zone) lookup and compression
- A learned **Zone Database** that stores winning/losing/drawing position-pattern matrices and provides a k-NN based zone score
- **Zone Guidance** integrated into PUCT (`λ-PUCT`) to bias MCTS toward favorable zones

The Breakthrough variant uses an internal 8×8 numpy board with lightweight move tuples `(fr, fc, tr, tc)`. Policy outputs are flattened 4096-length move logits (from-square * 64 + to-square), and the value net predicts game outcome in [-1,1].

## Files Overview

| File                        | Purpose |
|----------------------------|---------|
| `breakthrough_mcvs.py`     | Full MCVS implementation for Breakthrough: game logic, ABC/WeightedMatrix classes, Policy/Value CNNs, Zone DB, MCVS & UCT searchers, self-play and training loop. |
| `breakthrough_zone_db.npz` | Zone database file: stores Hilbert-ordered matrices for winning, losing, and draw zones used by zone guidance. Created/updated by `breakthrough_mcvs.py`. |


## Notes

- The policy network maps a 1×64×64 weighted matrix tensor to a 4096-dimensional logits vector for flat move indexing.
- The zone DB uses k-NN similarity (L1 normalized) across Hilbert-ordered matrices and returns a zone score Z ∈ [-1, 1].
- `breakthrough_mcvs.py` includes a training loop that performs self-play data generation, incremental training, checkpointing (`breakthrough_checkpoint.pt`) and periodic MCVS vs UCT evaluation.

For implementation details, inspect `breakthrough_mcvs.py`. If you want a shorter quick-start, ask me to add a minimal README usage section with run commands and environment notes.
