---
license: mit                          # Change if you use a different license
pretty_name: Breakthrough MCVS - Zone Guided AI
tags:
  - breakthrough
  - monte-carlo
  - reinforcement-learning
  - game-ai
  - numpy
  - adjacency-matrix
  - dataset
task_categories:
  - other
---

# Breakthrough MCVS - Zone Guided AI

**Advanced Monte-Carlo Value Search (MCVS) engine for the game Breakthrough**, powered by a novel **Displacement-based ABC Model** and **Weighted Adjacency Matrices**.

This repository contains the core AI model, training infrastructure, and a trained **Zone Database** used for position evaluation.

## Overview

This project implements a custom reinforcement learning agent for Breakthrough (8x8) using:
- Displacement-based ABC Model with homogeneous coordinates
- Dynamic Weighted Adjacency Matrices (A ⊙ S ⊙ F)
- Hilbert curve ordering for efficient zone retrieval
- Zone-guided Monte-Carlo Value Search (MCVS)

## Database Contents (`breakthrough_zone_db.npz`)

The zone database contains learned position patterns from self-play:

| Category     | Count  | Description                          |
|--------------|--------|--------------------------------------|
| Winning      | 2,097  | Positions leading to Player 1 victory |
| Losing       | 1,793  | Positions leading to Player 1 defeat  |
| Draw         | 0      | Draw positions (none yet)            |
| **Total**    | **3,890** | Stored game states                |

Each position is represented as a **64×64 weighted adjacency matrix**.

For more information please refer to the paper at: https://doi.org/10.13140/RG.2.2.18795.09764

### How to inspect the database

Run the included script:

```bash
python inspect_npz.py