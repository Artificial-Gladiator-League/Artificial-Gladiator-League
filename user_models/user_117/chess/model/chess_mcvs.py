# chess_mcvs.py
"""
chess_mcvs.py - Full implementation analogous to breakthrough_mcvs.py

Features:
- Chess game logic using python-chess (full rules)
- Fixed 64×64 padded weighted adjacency matrix (Hilbert-ordered pieces)
- Piece-specific kappa values
- Queen-only promotions for flat 4096 policy head compatibility
- Policy and Value CNNs (lightweight with pooling for feasibility)
- HilbertOrderedZoneDatabase with add methods and proper k-NN zone score
- MCVSSearcher (guided λ-PUCT with policy/value/zone)
- UCTSearcher (baseline)
- train_networks function
- Ready for chess_mcvs_vs_uct.py tournament

Requires: pip install chess torch numpy
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
import chess
import random
import time
import os
import bisect
from collections import Counter
from typing import List, Dict, Tuple

# ============================================================================
# HILBERT CURVE UTILITIES
# ============================================================================

def xy2d(n: int, x: int, y: int) -> int:
    d = 0
    s = n // 2
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = n - 1 - x
                y = n - 1 - y
            x, y = y, x
        s //= 2
    return d

def matrix_to_hilbert_index(W: np.ndarray) -> int:
    if W.size == 0:
        return 0
    W_flat = W.flatten()
    max_idx = np.argmax(W_flat)
    x = max_idx % 64
    y = max_idx // 64
    return xy2d(64, x, y)

# ============================================================================
# ABC MODEL & WEIGHTED MATRIX
# ============================================================================

from abc_model import ABCModelDynamic
from matrix_model import WeightedMatrixABC

# ============================================================================
# CHESS GAME LOGIC
# ============================================================================

class Chess:
    def __init__(self):
        self.board = chess.Board()
        self.move_count = 0
        self.position_history = Counter()
        self._update_position_key()

    def _update_position_key(self):
        key = (self.board.fen(), self.board.turn)
        self.position_history[key] += 1

    def copy(self):
        new_game = Chess()
        new_game.board = self.board.copy()
        new_game.move_count = self.move_count
        new_game.position_history = self.position_history.copy()
        return new_game

    def undo_move(self) -> None:
        """
        Undo the last move (pop) and revert bookkeeping (move_count, position_history).

        This complements `apply_move` which increments move_count and updates
        the position_history for the new state after a push. `undo_move` decrements
        the same position record and pops the board to restore the previous state.
        """
        # The current board fen/turn corresponds to the state that was last pushed;
        # decrement its count before popping to fully revert the update.
        key = (self.board.fen(), self.board.turn)
        if key in self.position_history:
            # decrement safely (don't remove key to avoid KeyError on repeated ops)
            self.position_history[key] = max(0, self.position_history[key] - 1)
        # pop the board and update counters
        self.board.pop()
        self.move_count = max(0, self.move_count - 1)

    def get_legal_moves(self) -> List[chess.Move]:
        moves = []
        for move in self.board.legal_moves:
            if move.promotion is None or move.promotion == chess.QUEEN:
                moves.append(move)
        return moves

    def apply_move(self, move: chess.Move) -> None:
        self.board.push(move)
        self.move_count += 1
        self._update_position_key()

    def is_terminal(self) -> bool:
        return self.board.is_game_over(claim_draw=True)

    def check_winner(self) -> int:
        if not self.is_terminal():
            return 0
        outcome = self.board.outcome(claim_draw=True)
        if outcome is None or outcome.winner is None:
            return 0
        return 1 if outcome.winner == chess.WHITE else -1

    def get_weighted_adjacency_matrix(self) -> np.ndarray:
        occupied = []
        for sq in chess.SQUARES:
            piece = self.board.piece_at(sq)
            if piece:
                file = chess.square_file(sq)
                rank = chess.square_rank(sq)
                x = float(file) - 3.5
                y = float(rank) - 3.5
                color_val = 1.0 if piece.color == chess.WHITE else 1.1
                type_val = {
                    chess.PAWN:   1.0,
                    chess.KNIGHT: 3.0,
                    chess.BISHOP: 3.2,
                    chess.ROOK:   5.0,
                    chess.QUEEN:  9.0,
                    chess.KING:  20.0
                }[piece.piece_type]
                kappa = np.array([1.0, color_val, type_val])
                hilbert_d = xy2d(8, file, rank)
                occupied.append((hilbert_d, (x, y), kappa))

        occupied.sort(key=lambda t: t[0])
    
        abc = ABCModelDynamic(n=2, t=1.0, T=1.41)
        for _, pos, kappa_vec in occupied:
            abc.add_piece(pos, delta_values=(1.0, kappa_vec[1], kappa_vec[2]), kappa_vector=kappa_vec)

        w_calc = WeightedMatrixABC(abc, sigma=1.0)
        W_var = w_calc.compute_weighted_matrix()

        num = len(occupied)
        W = np.zeros((64, 64))
        if num > 0:
            W[:num, :num] = W_var
        return W

# ============================================================================
# NEURAL NETWORKS
# ============================================================================

class PolicyNetworkCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(1, 64, kernel_size=5, padding=2, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 128, kernel_size=5, padding=2, stride=2),
            nn.ReLU(),
            nn.Conv2d(128, 128, kernel_size=3, padding=1, stride=2),
            nn.ReLU(),
        )
        self.fc = nn.Linear(128 * 8 * 8, 4096)

    def forward(self, x):
        x = self.conv(x)
        x = x.view(x.size(0), -1)
        return self.fc(x)

class ValueNetworkCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(1, 64, kernel_size=5, padding=2, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 128, kernel_size=5, padding=2, stride=2),
            nn.ReLU(),
            nn.Conv2d(128, 128, kernel_size=3, padding=1, stride=2),
            nn.ReLU(),
        )
        self.fc = nn.Sequential(
            nn.Linear(128 * 8 * 8, 512),
            nn.ReLU(),
            nn.Linear(512, 1),
            nn.Tanh()
        )

    def forward(self, x):
        x = self.conv(x)
        x = x.view(x.size(0), -1)
        return self.fc(x)

# ============================================================================
# ZONE DATABASE
# ============================================================================

class HilbertOrderedZoneDatabase:
    def __init__(self, filepath: str = "chess_zone_db.npz", max_size: int = 100000):
        self.filepath = filepath
        self.max_size = max_size  # Maximum matrices per zone
        self.winning_matrices = []
        self.winning_indices = []
        self.losing_matrices = []
        self.losing_indices = []
        self.draw_matrices = []
        self.draw_indices = []
        self.load()
    
    def add_winning_matrix(self, W: np.ndarray):
        if W.shape != (64, 64):
            return
        
        hilbert_idx = matrix_to_hilbert_index(W)
        pos = bisect.bisect_left(self.winning_indices, hilbert_idx)
        self.winning_indices.insert(pos, hilbert_idx)
        self.winning_matrices.insert(pos, W.copy())
    
    def add_losing_matrix(self, W: np.ndarray):
        if W.shape != (64, 64):
            return
        
        hilbert_idx = matrix_to_hilbert_index(W)
        pos = bisect.bisect_left(self.losing_indices, hilbert_idx)
        self.losing_indices.insert(pos, hilbert_idx)
        self.losing_matrices.insert(pos, W.copy()) 
    
    def add_draw_matrix(self, W: np.ndarray):
        if W.shape != (64, 64):
            return
        
        hilbert_idx = matrix_to_hilbert_index(W)
        pos = bisect.bisect_left(self.draw_indices, hilbert_idx)
        self.draw_indices.insert(pos, hilbert_idx)
        self.draw_matrices.insert(pos, W.copy())
    
    def add_game_record(self, trajectory: List[Chess], result: int, sample_rate: float = 0.3):
        """Add game record with sampling to control database size."""
        # Sample states: always keep first and last, random subset of middle
        sampled_states = []
        
        for i, state in enumerate(trajectory):
            if i == 0 or i == len(trajectory) - 1:
                # Always keep start and end positions
                sampled_states.append(state)
            elif random.random() < sample_rate:
                # Randomly sample middle positions
                sampled_states.append(state)
        
        for state in sampled_states:
            W = state.get_weighted_adjacency_matrix()
            
            if result == 1:
                self.add_winning_matrix(W)
            elif result == -1:
                self.add_losing_matrix(W)
            else:
                self.add_draw_matrix(W)
    
    def save(self):
        """Save database with pruning if too large."""
        # Prune if any zone exceeds 80% of max_size
        threshold = int(self.max_size * 0.8)
        
        if (len(self.winning_matrices) > threshold or 
            len(self.losing_matrices) > threshold or 
            len(self.draw_matrices) > threshold):
            print(f"Pruning database before save...")
            self.prune_database(target_size=int(self.max_size * 0.7))
        
        try:
            np.savez_compressed(
                self.filepath,
                winning=np.array(self.winning_matrices, dtype=object),
                winning_indices=np.array(self.winning_indices),
                losing=np.array(self.losing_matrices, dtype=object),
                losing_indices=np.array(self.losing_indices),
                draw=np.array(self.draw_matrices, dtype=object),
                draw_indices=np.array(self.draw_indices),
            )
            print(f"Zone DB saved: W={len(self.winning_matrices)}, L={len(self.losing_matrices)}, D={len(self.draw_matrices)}")
        except MemoryError:
            print(f"MemoryError during save! Pruning more aggressively...")
            self.prune_database(target_size=1000)
            # Try again with much smaller size
            np.savez_compressed(
                self.filepath,
                winning=np.array(self.winning_matrices, dtype=object),
                winning_indices=np.array(self.winning_indices),
                losing=np.array(self.losing_matrices, dtype=object),
                losing_indices=np.array(self.losing_indices),
                draw=np.array(self.draw_matrices, dtype=object),
                draw_indices=np.array(self.draw_indices),
            )
    
    def load(self):
        if os.path.exists(self.filepath):
            try:
                data = np.load(self.filepath, allow_pickle=True)
                self.winning_matrices = list(data['winning'])
                self.winning_indices = list(data['winning_indices'])
                self.losing_matrices = list(data['losing'])
                self.losing_indices = list(data['losing_indices'])
                self.draw_matrices = list(data['draw'])
                self.draw_indices = list(data['draw_indices'])
                print(f"Loaded zone DB: W={len(self.winning_matrices)}, L={len(self.losing_matrices)}, D={len(self.draw_matrices)}")
            except Exception as e:
                print(f"Failed to load zone database: {e}")
    
    def prune_database(self, target_size: int = 5000):
        """Keep only evenly-spaced diverse samples along Hilbert curve."""
        def prune_zone(matrices, indices, target):
            if len(matrices) <= target:
                return matrices, indices
            
            # Keep evenly spaced samples
            step = max(1, len(matrices) // target)
            keep_idx = list(range(0, len(matrices), step))[:target]
            
            new_matrices = [matrices[i] for i in keep_idx]
            new_indices = [indices[i] for i in keep_idx]
            
            return new_matrices, new_indices
        
        old_sizes = (len(self.winning_matrices), len(self.losing_matrices), len(self.draw_matrices))
        
        self.winning_matrices, self.winning_indices = prune_zone(
            self.winning_matrices, self.winning_indices, target_size
        )
        self.losing_matrices, self.losing_indices = prune_zone(
            self.losing_matrices, self.losing_indices, target_size
        )
        self.draw_matrices, self.draw_indices = prune_zone(
            self.draw_matrices, self.draw_indices, target_size
        )
        
        new_sizes = (len(self.winning_matrices), len(self.losing_matrices), len(self.draw_matrices))
        print(f"Pruned: W {old_sizes}→{new_sizes}, L {old_sizes}→{new_sizes}, D {old_sizes}→{new_sizes}")
    
    def compute_zone_score(self, W: np.ndarray, k: int = 5, beta: float = 0.5) -> float:
        """Compute zone guidance score Z(x(t), a) ∈ [-1, 1]."""
        if W.shape != (64, 64):
            return 0.0
        
        def knn_similarity(matrices, k_val):
            if len(matrices) == 0:
                return 0.0
            
            k_actual = min(k_val, len(matrices))
            distances = []
            
            for mat in matrices[:k_actual]:
                dist = np.sum(np.abs(W - mat)) / (64.0 * 64.0)
                distances.append(dist)
            
            similarities = [1.0 - d for d in distances]
            return np.mean(similarities)
        
        zone_win = knn_similarity(self.winning_matrices, k)
        zone_loss = knn_similarity(self.losing_matrices, k)
        zone_draw = knn_similarity(self.draw_matrices, k)
        
        Z = zone_win - zone_loss + beta * zone_draw
        return float(np.clip(Z, -1.0, 1.0))

# ============================================================================
# TRAINING FUNCTION
# ============================================================================

def train_networks(policy_net, value_net, W_states, policies, values, epochs=5, batch_size=32, device='cpu', lr=0.001):
    policy_net.train()
    value_net.train()
    opt_p = optim.Adam(policy_net.parameters(), lr=lr)
    opt_v = optim.Adam(value_net.parameters(), lr=lr)

    W_tensor = torch.tensor(np.array(W_states)[:, np.newaxis, :, :], dtype=torch.float32)
    pi_tensor = torch.tensor(np.array(policies), dtype=torch.float32)
    v_tensor = torch.tensor(np.array(values), dtype=torch.float32).unsqueeze(1)

    dataset = TensorDataset(W_tensor, pi_tensor, v_tensor)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    for epoch in range(epochs):
        total_loss = 0.0
        for batch_W, batch_pi, batch_v in loader:
            batch_W = batch_W.to(device)
            batch_pi = batch_pi.to(device)
            batch_v = batch_v.to(device)

            logits = policy_net(batch_W)
            # Fixed policy loss: KL divergence between network policy and MCTS visit distribution
            policy_loss = F.kl_div(F.log_softmax(logits, dim=1), batch_pi, reduction='batchmean')

            pred_v = value_net(batch_W)
            value_loss = F.mse_loss(pred_v, batch_v)

            loss = policy_loss + value_loss
            total_loss += loss.item()

            opt_p.zero_grad()
            opt_v.zero_grad()
            loss.backward()

            # Optional but recommended: gradient clipping
            torch.nn.utils.clip_grad_norm_(policy_net.parameters(), max_norm=1.0)
            torch.nn.utils.clip_grad_norm_(value_net.parameters(), max_norm=1.0)

            opt_p.step()
            opt_v.step()

        # Optional: print epoch loss for monitoring
        print(f"Epoch {epoch+1}/{epochs} - Loss: {total_loss/len(loader):.4f}")

# ============================================================================
# MCVS SEARCHER
# ============================================================================

class MCVSSearcher:
    def __init__(self, policy_net, value_net, zone_db, device='cpu',
                 cpuct=1.41, lambda_zone=0.0, k_zone=5, use_nets=True,
                 dirichlet_alpha: float = 0.3, dirichlet_noise_fraction: float = 0.25):
        self.policy_net = policy_net.to(device) if policy_net and use_nets else None
        self.value_net = value_net.to(device) if value_net and use_nets else None
        self.zone_db = zone_db
        self.device = device
        self.cpuct = cpuct
        self.lambda_zone = lambda_zone
        self.k_zone = k_zone
        self.use_nets = use_nets
        # Dirichlet noise hyperparameters (alpha for the Dirichlet, and fraction)
        self.dirichlet_alpha = float(dirichlet_alpha)
        self.dirichlet_noise_fraction = float(dirichlet_noise_fraction)

    def _rollout(self, game: Chess) -> float:
        """Simple random rollout for leaf evaluation when no value network.

        Uses push/pop (apply_move / undo_move) on the provided `game` to avoid
        allocating a full copy per rollout. The rollout will undo its own
        moves before returning.
        """
        depth = 0
        max_depth = 60  # reduced depth for faster rollouts (configurable)
        moves_played = []
        while not game.is_terminal() and depth < max_depth:
            moves = game.get_legal_moves()
            if not moves:
                break
            m = random.choice(moves)
            game.apply_move(m)
            moves_played.append(m)
            depth += 1

        winner = game.check_winner()

        # Undo rollout moves
        for _ in range(len(moves_played)):
            game.undo_move()

        return float(winner)  # +1, -1, or 0

    class Node:
        def __init__(self, prior: float = 0.0, zone: float = 0.0):
            self.prior = prior
            self.zone = zone
            self.visit_count = 0
            self.value_sum = 0.0
            self.children: Dict[chess.Move, 'MCVSSearcher.Node'] = {}

    def search_with_time_budget(self, game: Chess, time_budget: float) -> Tuple[Dict[chess.Move, int], int]:
        root = self.Node()
        start_time = time.time()
        simulations = 0

        while time.time() - start_time < time_budget:
            current_game = game
            node = root
            path = []
            moves_made = []

            # Selection
            while node.children:
                path.append(node)
                total_n = sum(c.visit_count for c in node.children.values()) + 1
                best_score = -float('inf')
                best_move = None
                best_child = None
                for move, child in node.children.items():
                    q = child.value_sum / child.visit_count if child.visit_count > 0 else 0.0
                    p_lambda = max(child.prior + self.lambda_zone * child.zone, 0.001)
                    u = self.cpuct * p_lambda * np.sqrt(total_n) / (1 + child.visit_count)
                    score = q + u
                    if score > best_score:
                        best_score = score
                        best_move = move
                        best_child = child
                current_game.apply_move(best_move)
                moves_made.append(best_move)
                node = best_child

            path.append(node)

            # Terminal node?
            if current_game.is_terminal():
                value = float(current_game.check_winner())
            else:
                # Expansion & Evaluation
                legal_moves = current_game.get_legal_moves()

                if self.use_nets and self.policy_net is not None and self.value_net is not None:
                    # === Neural network branch ===
                    W = current_game.get_weighted_adjacency_matrix()
                    W_tensor = torch.from_numpy(W)[None, None, :, :].float().to(self.device)
                    with torch.no_grad():
                        logits = self.policy_net(W_tensor)[0]
                        value = self.value_net(W_tensor)[0, 0].item()

                    probs = F.softmax(logits, dim=0).cpu().numpy()
                    prior_dict = {}
                    prior_sum = 0.0
                    for m in legal_moves:
                        idx = m.from_square * 64 + m.to_square
                        p = float(probs[idx])
                        prior_dict[m] = p
                        prior_sum += p
                    if prior_sum > 0.0:
                        for m in prior_dict:
                            prior_dict[m] /= prior_sum
                    else:
                        p = 1.0 / len(legal_moves)
                        prior_dict = {m: p for m in legal_moves}
                else:
                    # === Zone-only / no-nets branch ===
                    value = self._rollout(current_game)
                    prior_dict = {m: 1.0 / len(legal_moves) for m in legal_moves} if legal_moves else {}

                # Dirichlet noise only at root and only when using nets.
                # Use configured hyperparameters so the same alpha/fraction
                # can be applied to both priors and zone guidance.
                if len(path) == 1 and self.use_nets and legal_moves:
                    noise = np.random.dirichlet([self.dirichlet_alpha] * len(legal_moves))
                    for i, m in enumerate(legal_moves):
                        prior_dict[m] = (1 - self.dirichlet_noise_fraction) * prior_dict[m] + \
                                        self.dirichlet_noise_fraction * noise[i]
                    # Renormalize
                    prior_sum = sum(prior_dict.values())
                    if prior_sum > 0:
                        for m in prior_dict:
                            prior_dict[m] /= prior_sum

                # Create children with zone scores. Use apply/undo on the shared
                # board to avoid allocating a full copy per child. Skip heavy
                # weighted-matrix computations when zone guidance is disabled
                # (lambda_zone == 0) or when the DB is empty.
                zone_db_empty = (len(self.zone_db.winning_matrices) == 0 and
                                 len(self.zone_db.losing_matrices) == 0 and
                                 len(self.zone_db.draw_matrices) == 0)
                need_zone = (self.lambda_zone != 0.0) and (not zone_db_empty)

                for i, m in enumerate(legal_moves):
                    if need_zone:
                        current_game.apply_move(m)
                        child_W = current_game.get_weighted_adjacency_matrix()
                        child_Z = self.zone_db.compute_zone_score(child_W, k=self.k_zone)
                        current_game.undo_move()
                    else:
                        child_Z = 0.0

                    # If Dirichlet noise was added to priors at the root, apply the same
                    # noise (mapped to [-1,1]) to zone guidance so both signals share
                    # the same stochastic perturbation controlled by the same alpha.
                    if len(path) == 1 and self.use_nets and legal_moves:
                        z_noise = (noise[i] * 2.0) - 1.0  # map [0,1] -> [-1,1]
                        child_Z = (1 - self.dirichlet_noise_fraction) * child_Z + \
                                  self.dirichlet_noise_fraction * z_noise

                    child = self.Node(prior=prior_dict.get(m, 0.0), zone=child_Z)
                    node.children[m] = child

            # Backpropagation
            v = value
            for n in reversed(path):
                n.visit_count += 1
                n.value_sum += v
                v = -v

            # Undo moves that were applied during selection (the rollout
            # already undid its own moves). This restores `game` to the root
            # state for the next simulation.
            for _ in range(len(moves_made)):
                current_game.undo_move()

            simulations += 1

        visits = {move: child.visit_count for move, child in root.children.items()}
        if not visits:
            legal = game.get_legal_moves()
            visits = {random.choice(legal): 1} if legal else {}
        return visits, simulations

# ============================================================================
# UCT SEARCHER (your original from the provided file)
# ============================================================================

class UCTSearcher:
    def __init__(self, cpuct=np.sqrt(2.0)):
        self.cpuct = cpuct

    class Node:
        def __init__(self):
            self.visit_count = 0
            self.value_sum = 0.0
            self.children: Dict[chess.Move, 'UCTSearcher.Node'] = {}

    def _rollout(self, game: Chess) -> float:
        depth = 0
        max_depth = 60
        moves_played = []
        while not game.is_terminal() and depth < max_depth:
            moves = game.get_legal_moves()
            if not moves:
                break
            m = random.choice(moves)
            game.apply_move(m)
            moves_played.append(m)
            depth += 1

        if depth >= max_depth:
            # undo rollout moves
            for _ in range(len(moves_played)):
                game.undo_move()
            return 0.0

        winner = game.check_winner()

        # undo rollout moves
        for _ in range(len(moves_played)):
            game.undo_move()

        if winner == 1:
            return 1.0
        elif winner == -1:
            return -1.0
        else:
            return 0.0

    def search_with_time_budget(self, game: Chess, time_budget: float) -> Tuple[Dict[chess.Move, int], int]:
        root = self.Node()
        start_time = time.time()
        simulations = 0

        while time.time() - start_time < time_budget:
            current_game = game
            node = root
            path = [node]
            moves_made = []

            while True:
                legal_moves = current_game.get_legal_moves()

                if current_game.is_terminal():
                    winner = current_game.check_winner()
                    value = 1.0 if winner == 1 else -1.0 if winner == -1 else 0.0
                    break

                if not legal_moves:
                    value = 0.0
                    break

                unvisited_moves = [m for m in legal_moves if m not in node.children]
                if unvisited_moves:
                    move = random.choice(unvisited_moves)
                    child = self.Node()
                    node.children[move] = child
                    current_game.apply_move(move)
                    moves_made.append(move)
                    path.append(child)
                    value = self._rollout(current_game)
                    break

                best_score = -float('inf')
                best_move = None
                best_child = None
                total_visits = node.visit_count

                for move in legal_moves:
                    child = node.children[move]
                    Q = child.value_sum / (child.visit_count + 1e-8) if child.visit_count > 0 else 0.0
                    U = self.cpuct * np.sqrt(np.log(total_visits + 1) / (child.visit_count + 1e-8))
                    score = Q + U
                    if score > best_score:
                        best_score = score
                        best_move = move
                        best_child = child

                current_game.apply_move(best_move)
                moves_made.append(best_move)
                node = best_child
                path.append(node)

            for n in reversed(path):
                n.visit_count += 1
                n.value_sum += value
                value = -value

            # Undo selection moves to restore root state
            for _ in range(len(moves_made)):
                current_game.undo_move()

            simulations += 1

        visits = {move: child.visit_count for move, child in root.children.items()}
        if not visits:
            legal = game.get_legal_moves()
            return {random.choice(legal): 1} if legal else {}, simulations
        return visits, simulations

if __name__ == "__main__":
    print("chess_mcvs.py loaded successfully")