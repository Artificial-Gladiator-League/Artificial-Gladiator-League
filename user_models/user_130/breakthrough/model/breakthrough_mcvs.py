import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset
from typing import List, Dict, Tuple
import random
import time
import os
import bisect

from abc_model import ABCModelDynamic
from matrix_model import WeightedMatrixABC

# ============================================================================
# HILBERT CURVE UTILITIES
# ============================================================================

def xy2d(n: int, x: int, y: int) -> int:
    """Convert (x,y) to Hilbert curve distance."""
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
    """
    Convert 64x64 matrix to Hilbert curve index.
    """
    if W.size == 0:
        return 0
    
    # Find location of maximum value
    W_flat = W.flatten()
    max_idx = np.argmax(W_flat)
    
    x = max_idx % 64
    y = max_idx // 64
    idx = xy2d(64, x, y)
    # Sanity check: index must be in [0, 64*64-1]
    assert 0 <= idx < 64 * 64, f"Hilbert index out of range: {idx}"
    return idx


def move_to_index(fr: int, fc: int, tr: int, tc: int) -> int:
    """Convert a move tuple (fr,fc,tr,tc) to a policy index in [0,4095].

    Mapping: idx = (from_sq) * 64 + to_sq, where from_sq = fr*8 + fc, to_sq = tr*8 + tc
    """
    from_sq = fr * 8 + fc
    to_sq = tr * 8 + tc
    return from_sq * 64 + to_sq

# ============================================================================
# BREAKTHROUGH GAME LOGIC 
# ============================================================================

class Breakthrough:
    ROWS, COLS = 8, 8
    EMPTY, PLAYER1, PLAYER2 = 0, 1, 2

    def __init__(self):
        self.board = np.zeros((self.ROWS, self.COLS), dtype=np.int32)
        self.board[0:2, :] = self.PLAYER1
        self.board[6:8, :] = self.PLAYER2
        self.move_count = 0
        self.abc_model = ABCModelDynamic(n=2, t=1.0, T=1.41)
        self._cached_matrix = None
        self._setup_abc_initial()

    def _setup_abc_initial(self):
        """Initialize ABC model with starting pieces."""
        for row in range(2):
            for col in range(self.COLS):
                x = col - 3.5
                y = 3.5 - row
                self.abc_model.add_piece((x, y), (1.0, 1.0, 1.0))

        for row in range(6, 8):
            for col in range(self.COLS):
                x = col - 3.5
                y = 3.5 - row
                self.abc_model.add_piece((x, y), (1.0, 1.1, 1.0))

    def copy(self):
        """Create a deep copy of the game state."""
        new_game = Breakthrough()
        new_game.board = self.board.copy()
        new_game.move_count = self.move_count
        new_game.abc_model = ABCModelDynamic(n=2, t=1.0, T=1.41)
        
        for entry in self.abc_model.history:
            pos = entry['SB_position']
            # Preserve kappa (tokenized vector) if present
            kappa_vec = entry.get('kappa', None)
            new_game.abc_model.add_piece(pos, entry['delta'], kappa_vector=kappa_vec)
        
        new_game._cached_matrix = self._cached_matrix.copy() if self._cached_matrix is not None else None
        return new_game

    def get_legal_moves(self) -> List[Tuple[int, int, int, int]]:
        """Get all legal moves for the current player."""
        moves = []
        player = self.PLAYER1 if self.move_count % 2 == 0 else self.PLAYER2
        direction = 1 if player == self.PLAYER1 else -1

        for r in range(self.ROWS):
            for c in range(self.COLS):
                if self.board[r, c] == player:
                    # Forward move
                    nr = r + direction
                    if 0 <= nr < self.ROWS and self.board[nr, c] == self.EMPTY:
                        moves.append((r, c, nr, c))
                    
                    # Diagonal captures
                    for dc in [-1, 1]:
                        nc = c + dc
                        nr = r + direction
                        opponent = 3 - player
                        if 0 <= nr < self.ROWS and 0 <= nc < self.COLS and self.board[nr, nc] == opponent:
                            moves.append((r, c, nr, nc))
        
        return moves

    def apply_move(self, move: Tuple[int, int, int, int]) -> None:
        """Apply a move and update the ABC model."""
        fr, fc, tr, tc = move
        player = self.board[fr, fc]
        self.board[tr, tc] = player
        self.board[fr, fc] = self.EMPTY
        self.move_count += 1
        
        x = tc - 3.5
        y = 3.5 - tr 
        delta_2 = 1.0 if player == self.PLAYER1 else 1.1
        
        self.abc_model.add_piece((x, y), (1.0, delta_2, 1.0))
        self._cached_matrix = None

    def check_winner(self) -> int:
        """Check if there's a winner."""
        if np.any(self.board[0, :] == self.PLAYER2):
            return self.PLAYER2
        if np.any(self.board[7, :] == self.PLAYER1):
            return self.PLAYER1
        return self.EMPTY

    def is_terminal(self) -> bool:
        """Check if the game has ended."""
        return self.check_winner() != self.EMPTY or len(self.get_legal_moves()) == 0

    def get_weighted_adjacency_matrix(self) -> np.ndarray:
        if self._cached_matrix is not None:
            return self._cached_matrix

        occupied = []
        for r in range(8):
            for c in range(8):
                piece = self.board[r, c]
                if piece != self.EMPTY:
                    x = c - 3.5
                    y = 3.5 - r
                    delta_2 = 1.0 if piece == self.PLAYER1 else 1.1
                    kappa = (1.0, delta_2, 1.0)  # δ1=1.0 fixed, δ2=color, δ3=1.0 (no piece-type variation)
                    hilbert_d = xy2d(8, c, r)  # file=c, rank=r
                    occupied.append((hilbert_d, (x, y), kappa))

        occupied.sort(key=lambda t: t[0])  # Hilbert order

        abc = ABCModelDynamic(n=2, t=1.0, T=1.41)
        for _, pos, kappa_vec in occupied:
            abc.add_piece(pos, delta_values=kappa_vec)

        builder = WeightedMatrixABC(abc, sigma=1.0)
        W_small = builder.compute_weighted_matrix()

        n = len(occupied)
        W = np.zeros((64, 64), dtype=np.float32)
        if n > 0:
            W[:n, :n] = W_small

        self._cached_matrix = W
        return W

# ============================================================================
# NEURAL NETWORKS
# ============================================================================

class PolicyNetworkCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.relu = nn.ReLU()
        self.pool = nn.AdaptiveAvgPool2d(8)
        self.fc = nn.Linear(64 * 8 * 8, 4096)

    def forward(self, Wt):
        x = self.relu(self.conv1(Wt))
        x = self.relu(self.conv2(x))
        x = self.pool(x)
        x = x.view(x.size(0), -1)
        logits = self.fc(x)
        return logits


class ValueNetworkCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.relu = nn.ReLU()
        self.pool = nn.AdaptiveAvgPool2d(4)
        self.fc1 = nn.Linear(64 * 4 * 4, 64)
        self.fc2 = nn.Linear(64, 1)
        self.tanh = nn.Tanh()

    def forward(self, Wt):
        x = self.relu(self.conv1(Wt))
        x = self.relu(self.conv2(x))
        x = self.pool(x)
        x = x.view(x.size(0), -1)
        x = self.relu(self.fc1(x))
        value = self.tanh(self.fc2(x))
        return value


# ============================================================================
# ZONE DATABASE
# ============================================================================

class HilbertOrderedZoneDatabase:
    def __init__(self, filepath: str = "breakthrough_zone_db.npz", max_size: int = 10000):
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
    
    def add_game_record(self, trajectory: List[Breakthrough], result: int, sample_rate: float = 0.3):
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
# MCVS
# ============================================================================

class MCVSNode:
    def __init__(self, game: Breakthrough, parent=None, move=None):
        self.game = game.copy()
        self.parent = parent
        self.move = move
        self.children = {}
        self.N = 0
        self.Q = 0.0
        self.P = 0.0
        self.Z = 0.0  # Zone guidance score Z(x(t), a) ∈ [-1,1]

    def value(self) -> float:
        return self.Q / self.N if self.N > 0 else 0.0

class MCVSSearcher:
    class Node:
        def __init__(self, prior: float = 0.0, zone: float = 0.0):
            self.prior = prior
            self.zone = zone
            self.visit_count = 0
            self.value_sum = 0.0
            self.children = {}

    def __init__(self, policy_net=None, value_net=None, zone_db=None, device='cpu', 
                 cpuct=1.41, lambda_zone=0.0, k_zone=5, use_nets=True):
        self.policy_net = policy_net.to(device) if policy_net and use_nets else None
        self.value_net = value_net.to(device) if value_net and use_nets else None
        self.zone_db = zone_db
        self.device = device
        self.cpuct = cpuct
        self.lambda_zone = lambda_zone
        self.k_zone = k_zone
        self.use_nets = use_nets

    def _quick_rollout(self, game):
        current = game.copy()
        for _ in range(100):
            if current.is_terminal():
                break
            moves = current.get_legal_moves()
            if not moves:
                break
            current.apply_move(random.choice(moves))
        winner = current.check_winner()
        if winner == Breakthrough.PLAYER1:
            return 1.0
        elif winner == Breakthrough.PLAYER2:
            return -1.0
        else:
            return 0.0

    def search_with_time_budget(self, game, time_budget: float):
        root = self.Node()
        start_time = time.time()
        simulations = 0
        eps = 1e-6

        # If there are no legal moves at the root, return empty visits and zero sims
        root_legal = game.get_legal_moves()
        if not root_legal:
            return {}, 0

        while time.time() - start_time < time_budget:
            current_game = game.copy()
            node = root
            path = []

            # SELECTION
            while node.children:
                path.append(node)
                total_n = sum(c.visit_count for c in node.children.values()) + 1
                best_score = -float('inf')
                best_move = None
                best_child = None
                for move, child in node.children.items():
                    q = child.value_sum / child.visit_count if child.visit_count > 0 else 0.0
                    p_lambda = max(child.prior + self.lambda_zone * child.zone, eps)
                    u = self.cpuct * p_lambda * np.sqrt(total_n) / (1 + child.visit_count)
                    score = q + u
                    if score > best_score:
                        best_score = score
                        best_move = move
                        best_child = child
                # Advance game and node
                if best_move is None:
                    break
                current_game.apply_move(best_move)
                node = best_child

            path.append(node)

            # EXPANSION / EVALUATION
            if current_game.is_terminal():
                w = current_game.check_winner()
                if w == Breakthrough.PLAYER1:
                    value = 1.0
                elif w == Breakthrough.PLAYER2:
                    value = -1.0
                else:
                    value = 0.0
                legal_moves = []
                prior_dict = {}
            else:
                legal_moves = current_game.get_legal_moves()
                if not legal_moves:
                    prior_dict = {}
                    value = 0.0
                else:
                    if self.use_nets and self.policy_net is not None and self.value_net is not None:
                        W = current_game.get_weighted_adjacency_matrix()
                        W_tensor = torch.tensor(W, dtype=torch.float32).unsqueeze(0).unsqueeze(0).to(self.device)
                        with torch.no_grad():
                            logits = self.policy_net(W_tensor)
                            probs = torch.softmax(logits, dim=1).squeeze(0).cpu().numpy()
                            val = float(self.value_net(W_tensor).squeeze().cpu().numpy())
                            value = float(np.clip(val, -1.0, 1.0))

                        prior_dict = {}
                        total_p = 0.0
                        for m in legal_moves:
                            fr, fc, tr, tc = m
                            from_sq = fr * 8 + fc
                            to_sq = tr * 8 + tc
                            idx = from_sq * 64 + to_sq
                            p = float(probs[idx]) if idx < probs.size else 0.0
                            prior_dict[m] = p
                            total_p += p
                        if total_p <= 0.0:
                            prior_dict = {m: 1.0 / len(legal_moves) for m in legal_moves}
                        else:
                            for m in prior_dict:
                                prior_dict[m] /= total_p
                    else:
                        prior_dict = {m: 1.0 / len(legal_moves) for m in legal_moves}
                        value = self._quick_rollout(current_game)

            # EXPAND CHILDREN
            for m in legal_moves:
                child_game = current_game.copy()
                child_game.apply_move(m)
                child_W = child_game.get_weighted_adjacency_matrix()
                child_Z = self.zone_db.compute_zone_score(child_W, k=self.k_zone) if self.zone_db else 0.0
                child_prior = prior_dict.get(m, 1.0 / len(legal_moves))
                node_prior = max(child_prior + self.lambda_zone * child_Z, eps)
                node.children[m] = self.Node(prior=node_prior, zone=child_Z)

            # BACKPROPAGATION
            v = value
            for n in reversed(path):
                n.visit_count += 1
                n.value_sum += v
                v = -v

            simulations += 1

        visits = {move: child.visit_count for move, child in root.children.items()}
        if not visits:
            legal = game.get_legal_moves()
            visits = {random.choice(legal): 1} if legal else {}
        return visits, simulations

# ============================================================================
# UCT-MCTS (Baseline without guidance)
# ============================================================================

class UCTNode:
    def __init__(self, game: Breakthrough, parent=None, move=None):
        self.game = game.copy()
        self.parent = parent
        self.move = move
        self.children: Dict[Tuple[int,int,int,int], "UCTNode"] = {}
        self.N = 0          # visit count
        self.Q = 0.0        # total value

    def value(self) -> float:
        return self.Q / self.N if self.N > 0 else 0.0

class UCTSearcher:
    def __init__(self, cpuct: float = np.sqrt(2.0)):
        self.cpuct = cpuct

    def uct_score(self, parent: UCTNode, child: UCTNode) -> float:
        if child.N == 0:
            # Encourage exploration of unvisited nodes
            return float('inf')
        Q = child.value()
        U = self.cpuct * np.sqrt(parent.N + 1) / child.N
        return Q + U

    def simulate(self, game: Breakthrough) -> float:
        """
        Default policy: random playout to terminal state.
        Return +1 if PLAYER1 wins, -1 if PLAYER2 wins, 0 for draw.
        Value is from the perspective of the player to move at root,
        but since Breakthrough is alternating and we flip sign in backprop,
        we can just return game outcome from fixed perspective.
        """
        sim_game = game.copy()
        while not sim_game.is_terminal():
            moves = sim_game.get_legal_moves()
            if not moves:
                break
            move = random.choice(moves)
            sim_game.apply_move(move)
        winner = sim_game.check_winner()
        if winner == Breakthrough.PLAYER1:
            return 1.0
        elif winner == Breakthrough.PLAYER2:
            return -1.0
        else:
            return 0.0

    def search_with_time_budget(self, game: Breakthrough, time_budget: float) -> Dict[Tuple[int,int,int,int], int]:
        """
        Standard UCT search with a time budget (seconds).
        Returns a tuple: (move->visit count dict, simulations)
        """
        root = UCTNode(game)
        start_time = time.time()
        simulations = 0

        legal_moves = game.get_legal_moves()
        if not legal_moves:
            return {}, 0

        # Run simulations until time budget exhausted
        while time.time() - start_time < time_budget:
            node = root

            # SELECTION & EXPANSION
            while not node.game.is_terminal():
                legal = node.game.get_legal_moves()
                if not legal:
                    break

                # If any unexpanded moves exist, expand one
                if len(node.children) < len(legal):
                    untried = [m for m in legal if m not in node.children]
                    if untried:
                        move = random.choice(untried)
                        child_game = node.game.copy()
                        child_game.apply_move(move)
                        child = UCTNode(child_game, parent=node, move=move)
                        node.children[move] = child
                        node = child
                        break

                # Otherwise, select best child by UCT score
                best_move = max(node.children.keys(),
                                key=lambda m: self.uct_score(node, node.children[m]))
                node = node.children[best_move]

            # SIMULATION
            value = self.simulate(node.game)

            # BACKPROPAGATION
            current = node
            while current is not None:
                current.N += 1
                current.Q += value
                value = -value  # switch perspective
                current = current.parent
            simulations += 1
        return {move: child.N for move, child in root.children.items()}, simulations


# ============================================================================
# Self-play Data Generation
# ============================================================================

def generate_self_play_data(policy_net, value_net, zone_db, max_time_seconds=60.0,
                            lambda_zone=0.5, device='cpu'):
    """Generate self-play games using MCTS with zone guidance."""
    searcher = MCVSSearcher(policy_net, value_net, zone_db, device=device,
                           cpuct=1.41, lambda_zone=lambda_zone, k_zone=5)
    
    W_states = []
    policies = []
    values = []
    trajectories = []
    
    start_time = time.time()
    game_idx = 0
    
    while time.time() - start_time < max_time_seconds:
        game_start_time = time.time()
        game = Breakthrough()
        trajectory = [game.copy()]
        W_state_list = []
        policy_list = []
        
        while not game.is_terminal():
            time_per_move = 0.3  # Increased from 0.05 for more exploration/variety
            W_t = game.get_weighted_adjacency_matrix()
            visits, _ = searcher.search_with_time_budget(game, time_per_move)
            
            if not visits:
                break
            
            moves = list(visits.keys())
            visit_counts = np.array([visits[m] for m in moves], dtype=float)
            total_visits = visit_counts.sum()
            probs = visit_counts / total_visits
            
            # Add temperature for more variety in selection
            temperature = 1.0 if game.move_count < 8 else 0.25  # Higher early, lower late
            if temperature != 1.0:
                probs = probs ** (1.0 / temperature)
                probs = probs / probs.sum()
            
            policy_target = np.zeros(4096)
            for move, count in visits.items():
                fr, fc, tr, tc = move
                idx = move_to_index(fr, fc, tr, tc)
                if 0 <= idx < 4096:
                    policy_target[idx] = count / total_visits
            
            W_state_list.append(W_t)
            policy_list.append(policy_target)
            
            if len(moves) > 1:
                chosen_idx = np.random.choice(len(moves), p=probs)
                chosen_move = moves[chosen_idx]
            else:
                chosen_move = moves[0]
            
            game.apply_move(chosen_move)
            trajectory.append(game.copy())
        
        winner = game.check_winner()
        result = 1 if winner == Breakthrough.PLAYER1 else -1 if winner == Breakthrough.PLAYER2 else 0
        
        for i in range(len(W_state_list)):
            player_to_move = 1 if i % 2 == 0 else -1
            value = result if result == 0 else (result if result == player_to_move else -result)
            
            W_states.append(W_state_list[i])
            policies.append(policy_list[i])
            values.append(value)
        
        zone_db.add_game_record(trajectory, result)
        trajectories.append(trajectory)
        
        game_idx += 1
        game_duration = time.time() - game_start_time
        total_elapsed = time.time() - start_time
        
        print(f"Game {game_idx}: Winner={winner}, Moves={game.move_count}, Duration={game_duration:.1f}s, Total={total_elapsed:.1f}s")
    
    elapsed = time.time() - start_time
    print(f"\n✅ Self-play finished: {game_idx} games in {elapsed:.1f}s, {len(W_states)} training examples collected")
    
    return W_states, policies, values, trajectories


# ============================================================================
# Training
# ============================================================================

def train_networks(policy_net, value_net, W_states, policies, values, epochs=5, batch_size=32, device='cpu', learning_rate=None, lr=0.001):
    """Train policy and value networks.

    Accepts either `learning_rate=` (preferred) or `lr=` for backward compatibility.
    """
    if learning_rate is not None:
        lr = learning_rate

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
# Diagnostic Test for Move Diversity
# ============================================================================

def test_move_diversity(policy_net, value_net, zone_db, num_trials=30, time_budget=2.0, device='cpu'):
    """Test if the AI chooses different moves in the same starting position."""
    searcher = MCVSSearcher(policy_net, value_net, zone_db, device=device)
    game = Breakthrough()  # Starting position; change to any position you want to test
    
    move_counts = {}
    for i in range(num_trials):
        visits, _ = searcher.search_with_time_budget(game, time_budget)
        if visits:
            # Use argmax for the "best" move, but since there's noise/temp, it should vary
            best_move = max(visits, key=visits.get)
            move_counts[best_move] = move_counts.get(best_move, 0) + 1
    
    print("\nMove diversity test (starting position):")
    total = sum(move_counts.values())
    for m, cnt in sorted(move_counts.items(), key=lambda x: -x[1]):
        print(f"{m}: {cnt:2d}×  ({cnt/total:.1%})")
    print(f"Unique moves chosen: {len(move_counts)} out of {len(game.get_legal_moves())} legal\n")

# ============================================================================
# Guided MCVS vs UCT-MCTS Match
# ============================================================================

def play_match_mcvs_vs_uct(policy_net,
                           value_net,
                           zone_db,
                           num_games: int = 200,
                           time_per_move: float = 0.3,
                           device: str = 'cpu'):
    """
    Play a tournament between guided MCVS and UCT.
    Odd games: MCVS as PLAYER1 (first), UCT as PLAYER2.
    Even games: UCT as PLAYER1, MCVS as PLAYER2.
    """
    mcvs_searcher = MCVSSearcher(policy_net, value_net, zone_db,
                                 device=device, cpuct=1.41,
                                 lambda_zone=1.0, k_zone=5)
    uct_searcher = UCTSearcher(cpuct=np.sqrt(2.0))

    mcvs_wins = 0
    uct_wins = 0
    draws = 0

    for g in range(1, num_games + 1):
        game = Breakthrough()
        player_to_move = Breakthrough.PLAYER1  # always 1 then 2 then 1...

        # Alternate roles
        # True: MCVS plays PLAYER1 in this game
        mcvs_as_player1 = (g % 2 == 1)

        while not game.is_terminal():
            moves = game.get_legal_moves()
            if not moves:
                break

            # Decide which engine moves
            if player_to_move == Breakthrough.PLAYER1:
                use_mcvs = mcvs_as_player1
            else:
                use_mcvs = not mcvs_as_player1

            if use_mcvs:
                # Guided MCVS move
                visits, _ = mcvs_searcher.search_with_time_budget(game, time_per_move)
            else:
                # UCT move
                visits, _ = uct_searcher.search_with_time_budget(game, time_per_move)

            if not visits:
                break

            # Choose move by max visit count
            moves_list = list(visits.keys())
            counts = np.array([visits[m] for m in moves_list], dtype=float)
            best_index = int(np.argmax(counts))
            chosen_move = moves_list[best_index]

            game.apply_move(chosen_move)
            player_to_move = Breakthrough.PLAYER2 if player_to_move == Breakthrough.PLAYER1 else Breakthrough.PLAYER1

        winner = game.check_winner()
        if winner == Breakthrough.PLAYER1:
            if mcvs_as_player1:
                mcvs_wins += 1
            else:
                uct_wins += 1
        elif winner == Breakthrough.PLAYER2:
            if mcvs_as_player1:
                uct_wins += 1
            else:
                mcvs_wins += 1
        else:
            draws += 1

        print(f"Game {g}/{num_games} finished. Winner = {winner} "
              f"(MCVS as P1: {mcvs_as_player1})")

    print("\n=== MCVS vs UCT Tournament Results ===")
    print(f"Total games: {num_games}")
    print(f"MCVS wins : {mcvs_wins}")
    print(f"UCT wins  : {uct_wins}")
    print(f"Draws     : {draws}")
    if num_games > 0:
        mcvs_rate = mcvs_wins / num_games * 100.0
        uct_rate = uct_wins / num_games * 100.0
        draw_rate = draws / num_games * 100.0
        print(f"Win rates: MCVS={mcvs_rate:.1f}%, UCT={uct_rate:.1f}%, Draw={draw_rate:.1f}%\n")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    print("="*80)
    print("BREAKTHROUGH MCVS - INCREMENTAL TRAINING")
    print("="*80)
    
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    
    # Initialize networks
    policy_net = PolicyNetworkCNN().to(device)
    value_net = ValueNetworkCNN().to(device)
    
    # Initialize zone database (with fixed version - max_size limits growth)
    zone_db = HilbertOrderedZoneDatabase("breakthrough_zone_db.npz", max_size=10000)
    
    # Try to load existing checkpoint
    checkpoint_path = "breakthrough_checkpoint.pt"
    iteration = 0
    
    if os.path.exists(checkpoint_path):
        print(f"Loading checkpoint from {checkpoint_path}...")
        checkpoint = torch.load(checkpoint_path, map_location=device)
        policy_net.load_state_dict(checkpoint['policy'])
        value_net.load_state_dict(checkpoint['value'])
        iteration = checkpoint.get('iteration', 0)
        print(f"Resumed from iteration {iteration}")
    
    # Accumulate training data across iterations
    all_W_states = []
    all_policies = []
    all_values = []
    
    # Main training loop
    while True:
        print("="*80)
        print(f"ITERATION {iteration}")
        print("="*80)
        
        # 1. Generate self-play games (adds to zone_db automatically)
        print("Generating self-play data (30 minutes)...")
        lambda_zone = 0.0 if iteration == 0 else 1.0  # No guidance first iteration
        
        W_states, policies, values, trajectories = generate_self_play_data(
            policy_net, value_net, zone_db,
            max_time_seconds=1800.0,  # 30 minutes
            lambda_zone=lambda_zone,
            device=device
        )
        
        print(f"Collected {len(W_states)} training examples from {len(trajectories)} games")
        
        # 2. Accumulate training data
        all_W_states.extend(W_states)
        all_policies.extend(policies)
        all_values.extend(values)
        
        # Optional: Limit total training data size (keep most recent N examples)
        max_training_examples = 50000
        if len(all_W_states) > max_training_examples:
            print(f"Trimming training data to most recent {max_training_examples} examples...")
            all_W_states = all_W_states[-max_training_examples:]
            all_policies = all_policies[-max_training_examples:]
            all_values = all_values[-max_training_examples:]
        
        print(f"Total accumulated training examples: {len(all_W_states)}")
        
        # 3. Train networks on ALL accumulated data
        print("Training networks...")
        train_networks(
            policy_net, value_net,
            all_W_states, all_policies, all_values,
            epochs=10,  # Fewer epochs since we train more frequently
            device=device,
            learning_rate=0.001
        )
        
        # 4. Save checkpoint (ONE .pt file)
        print("Saving checkpoint...")
        torch.save({
            'policy': policy_net.state_dict(),
            'value': value_net.state_dict(),
            'iteration': iteration,
            'num_training_examples': len(all_W_states)
        }, checkpoint_path)
        print(f"✅ Checkpoint saved: {checkpoint_path}")
        
        # 5. Save zone database (ONE .npz file)
        zone_db.save()
        print(f"✅ Zone DB saved: W={len(zone_db.winning_matrices)}, "
              f"L={len(zone_db.losing_matrices)}, "
              f"D={len(zone_db.draw_matrices)}")
        
        iteration += 1
        
        print("\n" + "="*80)
        print(f"Iteration {iteration} complete!")
        print(f"Files updated:")
        print(f"  - {checkpoint_path}")
        print(f"  - {zone_db.filepath}")
        print("="*80 + "\n")
        
        # Optional: Run tournament evaluation every N iterations
        if iteration % 5 == 0:
            print("\nRunning tournament evaluation...")
            play_match_mcvs_vs_uct(
                policy_net, value_net, zone_db,
                num_games=20,
                time_per_move=0.5,
                device=device
            )
