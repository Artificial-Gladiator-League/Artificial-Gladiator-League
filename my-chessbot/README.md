---
license: mit
tags:
- chess
- game-ai
- pytorch
- safetensors
library_name: transformers
datasets:
- Maxlegrec/ChessFENS
---

# ChessBot Chess Model

This is a ChessBot model for chess move prediction and position evaluation. This model is way worse than stockfish. It is better than most humans however.
For stronger play, reducing temperature T (lower is stronger) is suggested.

## Model Description

The ChessBot model is a transformer-based architecture designed for chess gameplay. It can:
- Predict the next best move given a chess position (FEN)
- Evaluate chess positions
- Generate move probabilities

## Please Like if this model is useful to you :)

A like goes a long way !

## Usage

```python
import torch
from transformers import AutoModel

model = AutoModel.from_pretrained("Maxlegrec/ChessBot", trust_remote_code=True)
device = "cuda" if torch.cuda.is_available() else "cpu"
model = model.to(device)

# Example usage
fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Sample move from policy
move = model.get_move_from_fen_no_thinking(fen, T=0.1, device=device)
print(f"Policy-based move: {move}")
#e2e4

# Get the best move using value analysis
value_move = model.get_best_move_value(fen, T=0, device=device)
print(f"Value-based move: {value_move}")
#e2e4

# Get position evaluation
position_value = model.get_position_value(fen, device=device)
print(f"Position value [black_win, draw, white_win]: {position_value}")
#[0.2318, 0.4618, 0.3064]

# Get move probabilities
probs = model.get_move_from_fen_no_thinking(fen, T=1, device=device, return_probs=True)
top_moves = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:5]
print("Top 5 moves:")
for move, prob in top_moves:
    print(f"  {move}: {prob:.4f}")
#Top 5 moves:
#  e2e4: 0.9285
#  d2d4: 0.0712
#  g1f3: 0.0001
#  e2e3: 0.0000
#  c2c3: 0.0000
```

## Requirements

python-version <=3.11
cuda-version < 13.0

- torch>=2.0.0
- transformers>=4.48.1
- python-chess>=1.10.0
- numpy>=1.21.0

## Model Architecture

The architecture is strongly inspired from the LCzero project. Although written in pytorch.

- **Transformer layers**: 10
- **Hidden size**: 512
- **Feed-forward size**: 736
- **Attention heads**: 8
- **Vocabulary size**: 1929 (chess moves)

## Training Data

This model was trained on training data from the LCzero project. It consists of around 750M chess positions. I will publish the training dataset very soon.

## Limitations

- The model works best with standard chess positions
- Performance may vary with unusual or rare positions
- Requires GPU for optimal inference speed