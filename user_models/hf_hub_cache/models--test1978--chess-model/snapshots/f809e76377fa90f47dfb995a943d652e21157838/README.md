---
license: mit
pretty_name: Chess MCVS - Zone Guided AI
tags:
  - chess
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

# Chess MCVS - Zone Guided AI

**Advanced Monte-Carlo Value Search (MCVS)** engine for the game **Chess** (8x8), powered by a novel **Displacement-based ABC Model** and **Weighted Adjacency Matrices** with **Hilbert-ordered Zone Guidance**.

This repository implements a complete zone-guided reinforcement learning system, including self-play training, neural networks, and comparative tournaments against classic UCT.

## Core Idea

The engine uses:
- Displacement-based ABC Model with homogeneous coordinates
- Dynamic Weighted Adjacency Matrices `W = A ⊙ S ⊙ F`
- Hilbert curve ordering for efficient zone retrieval
- A learned **Zone Database** that stores winning/losing position patterns
- **Zone Guidance** (`λ-PUCT`) to bias search toward promising zones

For more information please refer to the paper at: https://doi.org/10.13140/RG.2.2.18795.09764

## Files Overview

| File                        | Purpose |
|----------------------------|--------|
| `chess_mcvs.py`            | Main implementation: game logic, ABC model, Zone Database, MCVS, neural networks, incremental training |

## Requirements

Install the minimal dependencies required to run `chess_mcvs.py` and the handler:

## Notes

The repository contains the following important file:
  - `chess_mcvs.py` — main implementation (game logic, ABC model, zone DB, MCVS, networks)

- For Hugging Face uploads, this `README.md` includes the model card front-matter (top YAML) and the `requirements.txt` lists the runtime dependencies.

