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

**Advanced Monte-Carlo Value Search (MCVS)** engine for the game **Breakthrough** (8x8), powered by a novel **Displacement-based ABC Model** and **Weighted Adjacency Matrices** with **Hilbert-ordered Zone Guidance**.

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
| `breakthrough_mcvs.py`     | Main implementation: game logic, ABC model, Zone Database, MCVS, neural networks, incremental training |
| `mcvs_vs_uct.py`           | 200-game tournament between MCVS and UCT with detailed logging and online learning |
| `abc_model.py`             | Displacement-based ABC Model |
| `matrix_model.py`          | Computes the weighted adjacency matrix |


## Requirements

## How to use:

A. Incremental Training

python breakthrough_mcvs.py

This runs continuous self-play + training:

1. Generates games using MCVS
2. Trains the neural networks
3. Updates and saves the Zone Database
4. Fully incremental (you can stop and resume anytime)

B. Tournament with Online Learning

This script runs tournament games while the AI learns (NN or just zone guidance).

# With Neural Networks (Full version – Online Learning):

bash: python mcvs_vs_uct.py

What happens:

1. MCVS plays against classic UCT (alternating sides)
2. Neural Policy and Value networks learn online after every game
3. Zone Database is updated and saved after each game
4. Zone guidance turns on automatically after game 1
5. Creates detailed logs:
6. breakthrough_full_results.txt — tournament summary
7. move_log.txt — per-move statistics
8. learning_log.txt — training progress

# Without Neural Networks (Zone-only Ablation)
To run the faster ablation version:

1. Open mcvs_vs_uct.py
2. Change this line near the bottom:

ablation_no_nets=False   # ← Change to True

```bash
pip install torch numpy

