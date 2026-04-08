"""
Example usage of the ChessBot Chess Model

This model can be used without installing any external packages except:
- torch
- transformers
- chess (python-chess)
- numpy
"""

import torch
import sys
sys.path.append("./")  # Add the model directory to path
from modeling_chessbot import ChessBotModel, ChessBotConfig

# Load the model
config = ChessBotConfig()
model = ChessBotModel.from_pretrained("./")

# Example usage
fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
device = "cuda" if torch.cuda.is_available() else "cpu"
model = model.to(device)

# Get the best move using policy
policy_move = model.get_move_from_fen_no_thinking(fen, T=0.1, device=device)
print(f"Policy-based move: {policy_move}")

# Get the best move using value analysis
value_move = model.get_best_move_value(fen, T=0.1, device=device)
print(f"Value-based move: {value_move}")

# Get position evaluation
position_value = model.get_position_value(fen, device=device)
print(f"Position value [black_win, draw, white_win]: {position_value}")

# Get move probabilities
probs = model.get_move_from_fen_no_thinking(fen, T=0.1, device=device, return_probs=True)
top_moves = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:5]
print("Top 5 moves:")
for move, prob in top_moves:
    print(f"  {move}: {prob:.4f}")
