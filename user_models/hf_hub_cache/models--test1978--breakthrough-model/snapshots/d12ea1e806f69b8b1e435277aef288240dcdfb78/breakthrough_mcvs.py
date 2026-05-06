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

# ============================================================================
# HILBERT CURVE UTILITIESggg
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
    if W.size == 0:
        return 0
    W_flat = W.flatten()
    max_idx = np.argmax(W_flat)
    x = max_idx % 64
    y = max_idx // 64
    return xy2d(64, x, y)


def move_to_index(fr: int, fc: int, tr: int, tc: int) -> int:
    """Convert move tuple (fr,fc,tr,tc) to policy index in [0,4095]."""
    from_sq = fr * 8 + fc
    to_sq = tr * 8 + tc
    return from_sq * 64 + to_sq

# ============================================================================
# ABC MODEL DYNAMIC
# ============================================================================

class ABCModelDynamic:
    """ABC Model with displacement-based B matrices."""

    def __init__(self, n: int = 2, t: float = 1.0, T: float = 1.41):
        self.n = n
        self.t = t
        self.T = T
        self.c0 = np.array([0.0, 0.0, 1.0])
        self.B_blocks = []
        self.piece_positions = []
        self.a_product = np.eye(3)
        self.delta = {}
        self.kappa = {}
        self.stage = 0
        self.MB_current = self.c0.copy()
        self.MB_previous = self.c0.copy()
        self.history = []

    def create_B_displacement(self, dx: float, dy: float) -> np.ndarray:
        return np.array([
            [1.0, 0.0, dx],
            [0.0, 1.0, dy],
            [0.0, 0.0, 1.0]
        ])

    def add_piece(self, position: tuple, delta_values: tuple, kappa_vector: np.ndarray = None):
        x, y = position
        current_MB = self.MB_current.copy()
        dx = x - current_MB[0]
        dy = y - current_MB[1]
        B_i = self.create_B_displacement(dx, dy)
        idx = len(self.B_blocks)
        self.B_blocks.append(B_i)
        self.piece_positions.append((x, y))
        self.a_product = self.a_product @ B_i
        self.MB_previous = self.MB_current.copy()
        self.MB_current = self.a_product @ self.c0
        self.delta[idx] = delta_values
        if kappa_vector is None:
            kappa_vector = np.array(delta_values)
        self.kappa[idx] = kappa_vector
        self.stage += 1
        self.history.append({
            'stage': self.stage,
            'SB_position': (x, y),
            'displacement': (dx, dy),
            'B_i': B_i.copy(),
            'a_t': self.a_product.copy(),
            'MB_prev': self.MB_previous.copy(),
            'MB_curr': self.MB_current.copy(),
            'delta': delta_values,
            'kappa': kappa_vector.copy()
        })

# ============================================================================
# WEIGHTED MATRIX ABC
# ============================================================================

class WeightedMatrixABC:
    """Compute weighted adjacency matrix from ABCModelDynamic."""

    def __init__(self, abc_model: ABCModelDynamic, sigma: float = 1.0):
        self.abc = abc_model
        self.n = abc_model.n
        self.t = abc_model.t
        self.T = abc_model.T
        self.sigma = sigma
        self.positions = None
        self.D = None
        self.A = None
        self.S = None
        self.F = None
        self.W = None

    def compute_piece_positions(self) -> np.ndarray:
        positions = []
        for pos in self.abc.piece_positions:
            x, y = pos
            positions.append(np.array([x, y, 1.0]))
        self.positions = np.array(positions)
        return self.positions

    def compute_distance_matrix(self) -> np.ndarray:
        if self.positions is None:
            self.compute_piece_positions()
        num = len(self.positions)
        D = np.zeros((num, num))
        for i in range(num):
            for j in range(num):
                pos_i = self.positions[i][:2]
                pos_j = self.positions[j][:2]
                D[i, j] = np.linalg.norm(pos_i - pos_j)
        self.D = D
        return D

    def compute_adjacency_matrix(self) -> np.ndarray:
        if self.D is None:
            self.compute_distance_matrix()
        num = len(self.positions)
        A = np.zeros((num, num))
        isolated = set()
        for i in range(num):
            has_neighbor = False
            for j in range(num):
                if i != j and self.t <= self.D[i, j] <= self.T:
                    has_neighbor = True
                    break
            if not has_neighbor:
                isolated.add(i)
        for i in range(num):
            for j in range(num):
                if i != j and self.t <= self.D[i, j] <= self.T:
                    A[i, j] = 1.0
                elif i == j and i in isolated:
                    A[i, j] = 1.0
        self.A = A
        return A

    def compute_spatial_matrix(self) -> np.ndarray:
        if self.D is None:
            self.compute_distance_matrix()
        num = len(self.positions)
        S = np.zeros((num, num))
        for i in range(num):
            for j in range(num):
                S[i, j] = np.exp(-self.D[i, j] ** 2 / (2 * self.sigma ** 2))
        self.S = S
        return S

    def compute_feature_matrix(self) -> np.ndarray:
        num = len(self.positions)
        F = np.zeros((num, num))
        kappas = []
        for i in range(num):
            if i in self.abc.kappa:
                kappas.append(self.abc.kappa[i])
            elif i in self.abc.delta:
                kappas.append(np.array(self.abc.delta[i]))
            else:
                kappas.append(np.array([1.0, 1.0, 1.0]))
        for i in range(num):
            for j in range(num):
                kappa_i = kappas[i]
                kappa_j = kappas[j]
                dot_product = np.dot(kappa_i, kappa_j)
                norm_i = np.linalg.norm(kappa_i)
                norm_j = np.linalg.norm(kappa_j)
                if norm_i > 0 and norm_j > 0:
                    F[i, j] = dot_product / (norm_i * norm_j)
                else:
                    F[i, j] = 0.0
        self.F = F
        return F

    def compute_weighted_matrix(self) -> np.ndarray:
        if self.A is None:
            self.compute_adjacency_matrix()
        if self.S is None:
            self.compute_spatial_matrix()
        if self.F is None:
            self.compute_feature_matrix()
        W = self.A * self.S * self.F
        self.W = W
        return W

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
        self._move_stack: List[Tuple] = []  # (fr, fc, tr, tc, captured)
        self._cached_matrix = None

    def copy(self):
        new_game = Breakthrough.__new__(Breakthrough)
        new_game.board = self.board.copy()
        new_game.move_count = self.move_count
        new_game._move_stack = list(self._move_stack)
        new_game._cached_matrix = (
            self._cached_matrix.copy() if self._cached_matrix is not None else None
        )
        return new_game

    def get_legal_moves(self) -> List[Tuple[int, int, int, int]]:
        moves = []
        player = self.PLAYER1 if self.move_count % 2 == 0 else self.PLAYER2
        direction = 1 if player == self.PLAYER1 else -1
        opponent = 3 - player
        for r in range(self.ROWS):
            for c in range(self.COLS):
                if self.board[r, c] == player:
                    nr = r + direction
                    if 0 <= nr < self.ROWS:
                        if self.board[nr, c] == self.EMPTY:
                            moves.append((r, c, nr, c))
                        for dc in [-1, 1]:
                            nc = c + dc
                            if 0 <= nc < self.COLS and self.board[nr, nc] == opponent:
                                moves.append((r, c, nr, nc))
        return moves

    def apply_move(self, move: Tuple[int, int, int, int]) -> None:
        fr, fc, tr, tc = move
        captured = int(self.board[tr, tc])
        self._move_stack.append((fr, fc, tr, tc, captured))
        self.board[tr, tc] = self.board[fr, fc]
        self.board[fr, fc] = self.EMPTY
        self.move_count += 1
        self._cached_matrix = None

    def undo_move(self) -> None:
        if not self._move_stack:
            return
        fr, fc, tr, tc, captured = self._move_stack.pop()
        self.board[fr, fc] = self.board[tr, tc]
        self.board[tr, tc] = captured
        self.move_count -= 1
        self._cached_matrix = None

    def check_winner(self) -> int:
        """Return PLAYER1 (1) if row 7 reached, PLAYER2 (2) if row 0 reached, else EMPTY (0)."""
        if np.any(self.board[7, :] == self.PLAYER1):
            return self.PLAYER1
        if np.any(self.board[0, :] == self.PLAYER2):
            return self.PLAYER2
        return self.EMPTY

    def is_terminal(self) -> bool:
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
                    kappa = np.array([1.0, delta_2, 1.0])
                    hilbert_d = xy2d(8, c, r)
                    occupied.append((hilbert_d, (x, y), kappa))

        occupied.sort(key=lambda t: t[0])

        abc = ABCModelDynamic(n=2, t=1.0, T=1.41)
        for _, pos, kappa_vec in occupied:
            abc.add_piece(pos, delta_values=(1.0, float(kappa_vec[1]), 1.0), kappa_vector=kappa_vec)

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
    def __init__(self, filepath: str = "breakthrough_zone_db.npz", max_size: int = 10000):
        self.filepath = filepath
        self.max_size = max_size
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
        sampled_states = []
        for i, state in enumerate(trajectory):
            if i == 0 or i == len(trajectory) - 1:
                sampled_states.append(state)
            elif random.random() < sample_rate:
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
        threshold = int(self.max_size * 0.8)
        if (len(self.winning_matrices) > threshold or
                len(self.losing_matrices) > threshold or
                len(self.draw_matrices) > threshold):
            print("Pruning database before save...")
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
            print("MemoryError during save! Pruning more aggressively...")
            self.prune_database(target_size=1000)
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
                if 'winning' in data and 'winning_indices' in data:
                    self.winning_matrices = list(data['winning'])
                    self.winning_indices = list(data['winning_indices'])
                    self.losing_matrices = list(data['losing'])
                    self.losing_indices = list(data['losing_indices'])
                    self.draw_matrices = list(data['draw'])
                    self.draw_indices = list(data['draw_indices'])
                elif 'winning_matrices' in data and 'winning_indices' in data:
                    self.winning_matrices = list(data['winning_matrices'])
                    self.winning_indices = list(data['winning_indices'])
                    self.losing_matrices = list(data['losing_matrices'])
                    self.losing_indices = list(data['losing_indices'])
                    self.draw_matrices = list(data['draw_matrices'])
                    self.draw_indices = list(data['draw_indices'])
                else:
                    try:
                        self.winning_matrices = list(data.get('winning', data.get('winning_matrices', [])))
                        self.winning_indices = list(data.get('winning_indices', []))
                        self.losing_matrices = list(data.get('losing', data.get('losing_matrices', [])))
                        self.losing_indices = list(data.get('losing_indices', []))
                        self.draw_matrices = list(data.get('draw', data.get('draw_matrices', [])))
                        self.draw_indices = list(data.get('draw_indices', []))
                    except Exception:
                        self.winning_matrices = []
                        self.winning_indices = []
                        self.losing_matrices = []
                        self.losing_indices = []
                        self.draw_matrices = []
                        self.draw_indices = []
                print(f"Loaded zone DB: W={len(self.winning_matrices)}, L={len(self.losing_matrices)}, D={len(self.draw_matrices)}")
            except Exception as e:
                print(f"Failed to load zone database: {e}")

    def prune_database(self, target_size: int = 5000):
        def prune_zone(matrices, indices, target):
            if len(matrices) <= target:
                return matrices, indices
            step = max(1, len(matrices) // target)
            keep_idx = list(range(0, len(matrices), step))[:target]
            return [matrices[i] for i in keep_idx], [indices[i] for i in keep_idx]

        old_sizes = (len(self.winning_matrices), len(self.losing_matrices), len(self.draw_matrices))
        self.winning_matrices, self.winning_indices = prune_zone(
            self.winning_matrices, self.winning_indices, target_size)
        self.losing_matrices, self.losing_indices = prune_zone(
            self.losing_matrices, self.losing_indices, target_size)
        self.draw_matrices, self.draw_indices = prune_zone(
            self.draw_matrices, self.draw_indices, target_size)
        new_sizes = (len(self.winning_matrices), len(self.losing_matrices), len(self.draw_matrices))
        print(f"Pruned: W {old_sizes[0]}->{new_sizes[0]}, L {old_sizes[1]}->{new_sizes[1]}, D {old_sizes[2]}->{new_sizes[2]}")

    def compute_zone_score(self, W: np.ndarray, k: int = 5, beta: float = 0.5) -> float:
        if W.shape != (64, 64):
            return 0.0

        def knn_similarity(matrices, k_val):
            if len(matrices) == 0:
                return 0.0
            k_actual = min(k_val, len(matrices))
            distances = [np.sum(np.abs(W - mat)) / (64.0 * 64.0) for mat in matrices[:k_actual]]
            return np.mean([1.0 - d for d in distances])

        zone_win = knn_similarity(self.winning_matrices, k)
        zone_loss = knn_similarity(self.losing_matrices, k)
        zone_draw = knn_similarity(self.draw_matrices, k)
        Z = zone_win - zone_loss + beta * zone_draw
        return float(np.clip(Z, -1.0, 1.0))

# ============================================================================
# TRAINING FUNCTION
# ============================================================================

def train_networks(policy_net, value_net, W_states, policies, values,
                   epochs=5, batch_size=32, device='cpu', lr=0.001):
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
            policy_loss = F.kl_div(F.log_softmax(logits, dim=1), batch_pi, reduction='batchmean')
            pred_v = value_net(batch_W)
            value_loss = F.mse_loss(pred_v, batch_v)

            loss = policy_loss + value_loss
            total_loss += loss.item()

            opt_p.zero_grad()
            opt_v.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy_net.parameters(), max_norm=1.0)
            torch.nn.utils.clip_grad_norm_(value_net.parameters(), max_norm=1.0)
            opt_p.step()
            opt_v.step()

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
        self.dirichlet_alpha = float(dirichlet_alpha)
        self.dirichlet_noise_fraction = float(dirichlet_noise_fraction)

    def _rollout(self, game: Breakthrough) -> float:
        """Random rollout using apply/undo for efficiency; leaves game at leaf state."""
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

        winner = game.check_winner()
        for _ in range(len(moves_played)):
            game.undo_move()

        if winner == Breakthrough.PLAYER1:
            return 1.0
        elif winner == Breakthrough.PLAYER2:
            return -1.0
        return 0.0

    class Node:
        def __init__(self, prior: float = 0.0, zone: float = 0.0):
            self.prior = prior
            self.zone = zone
            self.visit_count = 0
            self.value_sum = 0.0
            self.children: Dict[Tuple[int, int, int, int], 'MCVSSearcher.Node'] = {}

    def search_with_time_budget(self, game: Breakthrough, time_budget: float
                                ) -> Tuple[Dict[Tuple[int, int, int, int], int], int]:
        root = self.Node()
        start_time = time.time()
        simulations = 0

        if not game.get_legal_moves():
            return {}, 0

        while time.time() - start_time < time_budget:
            current_game = game          # shared reference — apply/undo restores state
            node = root
            path = []
            moves_made = []

            # ---- Selection ----
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

            # ---- Expansion & Evaluation ----
            if current_game.is_terminal():
                w = current_game.check_winner()
                value = 1.0 if w == Breakthrough.PLAYER1 else (-1.0 if w == Breakthrough.PLAYER2 else 0.0)
                legal_moves = []
            else:
                legal_moves = current_game.get_legal_moves()

                if self.use_nets and self.policy_net is not None and self.value_net is not None:
                    W = current_game.get_weighted_adjacency_matrix()
                    W_tensor = torch.from_numpy(W)[None, None, :, :].float().to(self.device)
                    with torch.no_grad():
                        logits = self.policy_net(W_tensor)[0]
                        value = self.value_net(W_tensor)[0, 0].item()

                    probs = F.softmax(logits, dim=0).cpu().numpy()
                    prior_dict = {}
                    prior_sum = 0.0
                    for m in legal_moves:
                        idx = move_to_index(*m)
                        p = float(probs[idx]) if idx < probs.size else 0.0
                        prior_dict[m] = p
                        prior_sum += p
                    if prior_sum > 0.0:
                        for m in prior_dict:
                            prior_dict[m] /= prior_sum
                    else:
                        p = 1.0 / len(legal_moves)
                        prior_dict = {m: p for m in legal_moves}
                else:
                    value = self._rollout(current_game)
                    prior_dict = {m: 1.0 / len(legal_moves) for m in legal_moves} if legal_moves else {}

                # Dirichlet noise at root when using nets
                noise = None
                if len(path) == 1 and self.use_nets and legal_moves:
                    noise = np.random.dirichlet([self.dirichlet_alpha] * len(legal_moves))
                    for i, m in enumerate(legal_moves):
                        prior_dict[m] = ((1 - self.dirichlet_noise_fraction) * prior_dict[m]
                                         + self.dirichlet_noise_fraction * noise[i])
                    prior_sum = sum(prior_dict.values())
                    if prior_sum > 0:
                        for m in prior_dict:
                            prior_dict[m] /= prior_sum

                zone_db_empty = (
                    self.zone_db is None or
                    (len(getattr(self.zone_db, 'winning_matrices', [])) == 0 and
                     len(getattr(self.zone_db, 'losing_matrices', [])) == 0 and
                     len(getattr(self.zone_db, 'draw_matrices', [])) == 0)
                )
                need_zone = (self.lambda_zone != 0.0) and (not zone_db_empty)

                for i, m in enumerate(legal_moves):
                    if need_zone:
                        current_game.apply_move(m)
                        child_W = current_game.get_weighted_adjacency_matrix()
                        child_Z = self.zone_db.compute_zone_score(child_W, k=self.k_zone)
                        current_game.undo_move()
                    else:
                        child_Z = 0.0

                    if len(path) == 1 and self.use_nets and legal_moves and noise is not None:
                        z_noise = (noise[i] * 2.0) - 1.0
                        child_Z = ((1 - self.dirichlet_noise_fraction) * child_Z
                                   + self.dirichlet_noise_fraction * z_noise)

                    node.children[m] = self.Node(prior=prior_dict.get(m, 0.0), zone=child_Z)

            # ---- Backpropagation ----
            v = value
            for n in reversed(path):
                n.visit_count += 1
                n.value_sum += v
                v = -v

            # Restore game to root state
            for _ in range(len(moves_made)):
                current_game.undo_move()

            simulations += 1

        visits = {move: child.visit_count for move, child in root.children.items()}
        if not visits:
            legal = game.get_legal_moves()
            visits = {random.choice(legal): 1} if legal else {}
        return visits, simulations

# ============================================================================
# UCT SEARCHER (baseline)
# ============================================================================

class UCTSearcher:
    def __init__(self, cpuct: float = np.sqrt(2.0)):
        self.cpuct = cpuct

    class Node:
        def __init__(self):
            self.visit_count = 0
            self.value_sum = 0.0
            self.children: Dict[Tuple[int, int, int, int], 'UCTSearcher.Node'] = {}

    def _rollout(self, game: Breakthrough) -> float:
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
            for _ in range(len(moves_played)):
                game.undo_move()
            return 0.0

        winner = game.check_winner()
        for _ in range(len(moves_played)):
            game.undo_move()

        if winner == Breakthrough.PLAYER1:
            return 1.0
        elif winner == Breakthrough.PLAYER2:
            return -1.0
        return 0.0

    def search_with_time_budget(self, game: Breakthrough, time_budget: float
                                ) -> Tuple[Dict[Tuple[int, int, int, int], int], int]:
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
                    w = current_game.check_winner()
                    value = 1.0 if w == Breakthrough.PLAYER1 else (-1.0 if w == Breakthrough.PLAYER2 else 0.0)
                    break

                if not legal_moves:
                    value = 0.0
                    break

                unvisited = [m for m in legal_moves if m not in node.children]
                if unvisited:
                    move = random.choice(unvisited)
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
                    Q = child.value_sum / (child.visit_count + 1e-8)
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

            for _ in range(len(moves_made)):
                current_game.undo_move()

            simulations += 1

        visits = {move: child.visit_count for move, child in root.children.items()}
        if not visits:
            legal = game.get_legal_moves()
            return {random.choice(legal): 1} if legal else {}, simulations
        return visits, simulations

# ============================================================================
# SELF-PLAY DATA GENERATION
# ============================================================================

def generate_self_play_data(policy_net, value_net, zone_db, max_time_seconds=60.0,
                            lambda_zone=0.5, device='cpu'):
    """Generate self-play games using MCVS with zone guidance."""
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
            time_per_move = 0.3
            W_t = game.get_weighted_adjacency_matrix()
            visits, _ = searcher.search_with_time_budget(game, time_per_move)

            if not visits:
                break

            moves = list(visits.keys())
            visit_counts = np.array([visits[m] for m in moves], dtype=float)
            total_visits = visit_counts.sum()
            if total_visits <= 0:
                break
            probs = visit_counts / total_visits

            temperature = 1.0 if game.move_count < 8 else 0.25
            if temperature != 1.0:
                probs = probs ** (1.0 / temperature)
                prob_sum = probs.sum()
                if prob_sum <= 0:
                    probs = np.ones(len(moves)) / len(moves)
                else:
                    probs = probs / prob_sum

            policy_target = np.zeros(4096)
            for move, count in visits.items():
                idx = move_to_index(*move)
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
        result = (1 if winner == Breakthrough.PLAYER1
                  else -1 if winner == Breakthrough.PLAYER2
                  else 0)

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
        print(f"Game {game_idx}: Winner={winner}, Moves={game.move_count}, "
              f"Duration={game_duration:.1f}s, Total={total_elapsed:.1f}s")

    elapsed = time.time() - start_time
    print(f"\nSelf-play finished: {game_idx} games in {elapsed:.1f}s, "
          f"{len(W_states)} training examples collected")

    return W_states, policies, values, trajectories

# ============================================================================
# DIAGNOSTIC TEST FOR MOVE DIVERSITY
# ============================================================================

def test_move_diversity(policy_net, value_net, zone_db, num_trials=30,
                        time_budget=2.0, device='cpu'):
    searcher = MCVSSearcher(policy_net, value_net, zone_db, device=device)
    game = Breakthrough()

    move_counts = {}
    for _ in range(num_trials):
        visits, _ = searcher.search_with_time_budget(game, time_budget)
        if visits:
            best_move = max(visits, key=visits.get)
            move_counts[best_move] = move_counts.get(best_move, 0) + 1

    print("\nMove diversity test (starting position):")
    total = sum(move_counts.values())
    for m, cnt in sorted(move_counts.items(), key=lambda x: -x[1]):
        print(f"{m}: {cnt:2d}x  ({cnt/total:.1%})")
    print(f"Unique moves chosen: {len(move_counts)} out of {len(game.get_legal_moves())} legal\n")

# ============================================================================
# GUIDED MCVS vs UCT-MCTS TOURNAMENT
# ============================================================================

def play_match_mcvs_vs_uct(policy_net, value_net, zone_db, num_games: int = 200,
                           time_per_move: float = 0.3, device: str = 'cpu'):
    """
    Tournament between guided MCVS and UCT.
    Odd games: MCVS as PLAYER1; even games: UCT as PLAYER1.
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
        mcvs_as_player1 = (g % 2 == 1)

        while not game.is_terminal():
            moves = game.get_legal_moves()
            if not moves:
                break

            is_player1_turn = (game.move_count % 2 == 0)
            use_mcvs = mcvs_as_player1 if is_player1_turn else not mcvs_as_player1

            if use_mcvs:
                visits, _ = mcvs_searcher.search_with_time_budget(game, time_per_move)
            else:
                visits, _ = uct_searcher.search_with_time_budget(game, time_per_move)

            if not visits:
                break

            moves_list = list(visits.keys())
            counts = np.array([visits[m] for m in moves_list], dtype=float)
            chosen_move = moves_list[int(np.argmax(counts))]
            game.apply_move(chosen_move)

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

        print(f"Game {g}/{num_games} finished. Winner={winner} (MCVS as P1: {mcvs_as_player1})")

    print("\n=== MCVS vs UCT Tournament Results ===")
    print(f"Total games : {num_games}")
    print(f"MCVS wins  : {mcvs_wins}")
    print(f"UCT wins   : {uct_wins}")
    print(f"Draws      : {draws}")
    if num_games > 0:
        print(f"Win rates  : MCVS={mcvs_wins/num_games*100:.1f}%, "
              f"UCT={uct_wins/num_games*100:.1f}%, Draw={draws/num_games*100:.1f}%\n")

def main():
    print("=" * 80)
    print("BREAKTHROUGH MCVS - INCREMENTAL TRAINING")
    print("=" * 80)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    policy_net = PolicyNetworkCNN().to(device)
    value_net = ValueNetworkCNN().to(device)

    zone_db = HilbertOrderedZoneDatabase("breakthrough_zone_db.npz", max_size=10000)

    checkpoint_path = "breakthrough_checkpoint.pt"
    iteration = 0

    if os.path.exists(checkpoint_path):
        print(f"Loading checkpoint from {checkpoint_path}...")
        checkpoint = torch.load(checkpoint_path, map_location=device)
        policy_net.load_state_dict(checkpoint['policy'])
        value_net.load_state_dict(checkpoint['value'])
        iteration = checkpoint.get('iteration', 0)
        print(f"Resumed from iteration {iteration}")

    all_W_states: List = []
    all_policies: List = []
    all_values: List = []

    while True:
        print("=" * 80)
        print(f"ITERATION {iteration}")
        print("=" * 80)

        print("Generating self-play data (30 minutes)...")
        lambda_zone = 0.0 if iteration == 0 else 1.0

        W_states, policies, values, trajectories = generate_self_play_data(
            policy_net, value_net, zone_db,
            max_time_seconds=1800.0,
            lambda_zone=lambda_zone,
            device=device
        )

        print(f"Collected {len(W_states)} training examples from {len(trajectories)} games")

        all_W_states.extend(W_states)
        all_policies.extend(policies)
        all_values.extend(values)

        max_training_examples = 50000
        if len(all_W_states) > max_training_examples:
            print(f"Trimming training data to most recent {max_training_examples} examples...")
            all_W_states = all_W_states[-max_training_examples:]
            all_policies = all_policies[-max_training_examples:]
            all_values = all_values[-max_training_examples:]

        print(f"Total accumulated training examples: {len(all_W_states)}")

        print("Training networks...")
        train_networks(
            policy_net, value_net,
            all_W_states, all_policies, all_values,
            epochs=10,
            device=device,
            lr=0.001
        )

        print("Saving checkpoint...")
        torch.save({
            'policy': policy_net.state_dict(),
            'value': value_net.state_dict(),
            'iteration': iteration,
            'num_training_examples': len(all_W_states)
        }, checkpoint_path)
        print(f"Checkpoint saved: {checkpoint_path}")

        zone_db.save()
        print(f"Zone DB saved: W={len(zone_db.winning_matrices)}, "
              f"L={len(zone_db.losing_matrices)}, D={len(zone_db.draw_matrices)}")

        iteration += 1

        print("\n" + "=" * 80)
        print(f"Iteration {iteration} complete!")
        print(f"Files updated: {checkpoint_path}, {zone_db.filepath}")
        print("=" * 80 + "\n")

        if iteration % 5 == 0:
            print("\nRunning tournament evaluation...")
            play_match_mcvs_vs_uct(
                policy_net, value_net, zone_db,
                num_games=20,
                time_per_move=0.5,
                device=device
            )


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
   pass