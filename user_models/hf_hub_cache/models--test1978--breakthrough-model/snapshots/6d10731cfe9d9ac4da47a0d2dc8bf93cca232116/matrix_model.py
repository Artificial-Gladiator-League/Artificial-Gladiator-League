"""
Matrix Model - TRULY FINAL CORRECTED Implementation
ABC Model with Weighted Adjacency Matrix (THREE components)

CRITICAL INSIGHT FROM abc_model.py:
    - piece_positions stored as (x, y) tuples
    - BUT MB_current = a_product @ c0 produces [x, y, 1] homogeneous coords
    - These are the SAME locations!
    
For matrix computations with B matrices, we MUST use homogeneous coordinates [x, y, 1]
to match the algebraic structure.

Implements Definition from Section 6:
    W[i,j](t) = A[i,j](t) ⊙ S[i,j](t) ⊙ F[i,j](t)

Author: Chaim Duchovny
Date: January 25, 2026 (TRULY FINAL CORRECTED)
"""

import numpy as np
from typing import Tuple

class ABCModelDynamic:
    """ABC Model with displacement-based B matrices."""
    
    def __init__(self, n: int = 2, t: float = 1.0, T: float = 1.41):
        """Initialize for 2D board."""
        self.n = n
        self.t = t
        self.T = T
        self.c0 = np.array([0.0, 0.0, 1.0])
        self.B_blocks = []
        self.piece_positions = []  # Stores (x, y) tuples
        self.a_product = np.eye(3)
        self.kappa = {}
        self.delta = {}
        self.stage = 0
        self.MB_current = self.c0.copy()
        self.MB_previous = self.c0.copy()
        self.history = []
    
    def create_B_displacement(self, dx: float, dy: float) -> np.ndarray:
        """Create displacement matrix B."""
        B = np.array([
            [1.0, 0.0, dx],
            [0.0, 1.0, dy],
            [0.0, 0.0, 1.0]
        ])
        return B
    
    def add_piece(self, position: tuple, delta_values: tuple, kappa_vector: np.ndarray = None):
        """
        Add a piece at position with delta values and tokenized vector.
        
        Args:
            position: (x, y) tuple - 2D coordinates from board
            delta_values: (δ₁, δ₂, δ₃) tuple
            kappa_vector: tokenized vector κ(B_i)
        """
        x, y = position
        current_MB = self.MB_current.copy()
        dx = x - current_MB[0]
        dy = y - current_MB[1]
        
        B_i = self.create_B_displacement(dx, dy)
        idx = len(self.B_blocks)
        
        self.B_blocks.append(B_i)
        self.piece_positions.append((x, y))  # Store as 2D tuple (board representation)
        self.a_product = self.a_product @ B_i
        
        self.MB_previous = self.MB_current.copy()
        self.MB_current = self.a_product @ self.c0  # Homogeneous coords [x, y, 1]
        
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
            'kappa': kappa_vector.copy() if kappa_vector is not None else None
        })


class WeightedMatrixABC:
    """
    Compute weighted adjacency matrix from ABCModelDynamic.
    
    CRITICAL: Converts (x,y) tuples to homogeneous coords [x,y,1] 
    to match algebraic structure of B matrices from abc_model.py
    
    Implements Definition from Section 6:
        W[i,j](t) = A[i,j](t) ⊙ S[i,j](t) ⊙ F[i,j](t)
    """
    
    def __init__(self, abc_model: ABCModelDynamic, sigma: float = 1.0):
        """
        Initialize with ABC model instance.
        
        Args:
            abc_model: ABCModelDynamic instance
            sigma: Standard deviation for Gaussian spatial kernel
        """
        self.abc = abc_model
        self.n = abc_model.n
        self.t = abc_model.t
        self.T = abc_model.T
        self.sigma = sigma
        self.positions = None  # Will store homogeneous coords [x, y, 1]
        self.D = None
        self.A = None
        self.S = None
        self.F = None
        self.W = None
    
    def compute_piece_positions(self) -> np.ndarray:
        """
        Extract positions from ABC model and convert to homogeneous coordinates.
        
        In abc_model.py:
            - piece_positions are (x, y) tuples
            - MB_current = a_product @ c0 produces [x, y, 1]
        
        For matrix computations, convert to homogeneous coords to match
        the algebraic structure of B matrices.
        
        Returns: Array of shape (num_pieces, 3) with homogeneous coords [x, y, 1]
        """
        positions = []
        for pos in self.abc.piece_positions:
            # pos is (x, y) from piece_positions
            x, y = pos
            # Convert to homogeneous coordinates to align with B matrix algebra
            positions.append(np.array([x, y, 1.0]))
        
        self.positions = np.array(positions)
        return self.positions
    
    def compute_distance_matrix(self) -> np.ndarray:
        """
        Compute pairwise Euclidean distances using 2D components only.
        
        Even though positions are [x, y, 1], we compute distance using only [x, y]
        D[i,j] = ||[x_i, y_i] - [x_j, y_j]||₂
        """
        if self.positions is None:
            self.compute_piece_positions()
        
        num = len(self.positions)
        D = np.zeros((num, num))
        
        for i in range(num):
            for j in range(num):
                # Use only 2D coordinates for distance
                pos_i = self.positions[i][:2]
                pos_j = self.positions[j][:2]
                D[i, j] = np.linalg.norm(pos_i - pos_j)
        
        self.D = D
        return D
    
    def compute_adjacency_matrix(self) -> np.ndarray:
        """
        Compute Adjacency Matrix (Definition 6.2).
        
        A[i,j] = 1 if:
            - k ≤ D[i,j] ≤ K (grid-adjacent)
            - i == j AND i is isolated (no neighbors in [k,K])
        
        A[i,j] = 0 otherwise
        """
        if self.D is None:
            self.compute_distance_matrix()
        
        num = len(self.positions)
        A = np.zeros((num, num))
        
        # Identify isolated pieces
        isolated = set()
        for i in range(num):
            has_neighbor = False
            for j in range(num):
                if i != j and self.t <= self.D[i, j] <= self.T:
                    has_neighbor = True
                    break
            if not has_neighbor:
                isolated.add(i)
        
        # Fill adjacency matrix
        for i in range(num):
            for j in range(num):
                # Check adjacency distance
                if i != j and self.t <= self.D[i, j] <= self.T:
                    A[i, j] = 1.0
                # Check if isolated piece
                elif i == j and i in isolated:
                    A[i, j] = 1.0
        
        self.A = A
        return A
    
    def compute_spatial_matrix(self) -> np.ndarray:
        """
        Compute Spatial Matrix (Definition 6.3).
        
        S[i,j](t) = exp(-||c_i(t) - c_j(t)||² / (2σ²))
        
        Gaussian kernel based on Euclidean distance.
        """
        if self.D is None:
            self.compute_distance_matrix()
        
        num = len(self.positions)
        S = np.zeros((num, num))
        
        for i in range(num):
            for j in range(num):
                S[i, j] = np.exp(-self.D[i, j]**2 / (2 * self.sigma**2))
        
        self.S = S
        return S
    
    def compute_feature_matrix(self) -> np.ndarray:
        """
        Compute Feature Matrix (Definition 6.4).
        
        F[i,j](t) = <κ(B_i), κ(B_j)> / (||κ(B_i)|| · ||κ(B_j)||)
        
        Cosine similarity between tokenized vectors.
        """
        num = len(self.positions)
        F = np.zeros((num, num))
        
        # Extract tokenized vectors
        kappas = []
        for i in range(num):
            if i in self.abc.kappa:
                kappas.append(self.abc.kappa[i])
            else:
                # Default: use delta as feature vector
                if i in self.abc.delta:
                    kappas.append(np.array(self.abc.delta[i]))
                else:
                    kappas.append(np.array([1.0, 1.0, 1.0]))
        
        # Compute cosine similarity
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
        """
        Compute Weighted Matrix (Definition from Section 6).
        
        W[i,j](t) = A[i,j](t) ⊙ S[i,j](t) ⊙ F[i,j](t)
        
        Returns: W - the full weighted matrix
        """
        if self.A is None:
            self.compute_adjacency_matrix()
        if self.S is None:
            self.compute_spatial_matrix()
        if self.F is None:
            self.compute_feature_matrix()
        
        # Hadamard product (element-wise multiplication)
        W = self.A * self.S * self.F
        
        self.W = W
        return W
    
    def compute_manhattan_distance(self, other: 'WeightedMatrixABC') -> float:
        """
        Compute Manhattan distance between two weighted matrices.
        
        distance = ||W_1 - W_2||₁
        """
        if self.W is None:
            self.compute_weighted_matrix()
        if other.W is None:
            other.compute_weighted_matrix()
        
        W1 = self.W
        W2 = other.W
        
        # Pad to same size
        size = max(len(W1), len(W2))
        W1_padded = np.zeros((size, size))
        W2_padded = np.zeros((size, size))
        W1_padded[:len(W1), :len(W1)] = W1
        W2_padded[:len(W2), :len(W2)] = W2
        
        return np.linalg.norm(W1_padded - W2_padded, ord=1)


# ============================================================================
# EXAMPLE 1: Two Adjacent Pieces (matches abc_model.py format)
# ============================================================================

def example_1():
    """Two adjacent pieces - exactly like abc_model.py."""
    print("\n" + "="*80)
    print("EXAMPLE 1: Two Adjacent Pieces")
    print("="*80)
    
    game = ABCModelDynamic(n=2, t=1.0, T=1.41)
    
    # Add pieces exactly like abc_model.py does
    game.add_piece((1.0, 0.0), (1.0, 1.0, 1.0), 
                   kappa_vector=np.array([1.0, 1.0, 1.0]))
    
    game.add_piece((1.0, 1.0), (1.0, 1.1, 1.0),
                   kappa_vector=np.array([1.0, 1.1, 1.0]))
    
    print("\nABC Model piece_positions (as stored in abc_model.py):")
    print(f"  Piece 0: {game.piece_positions[0]} (tuple from board)")
    print(f"  Piece 1: {game.piece_positions[1]} (tuple from board)")
    
    print("\nABC Model MB positions (homogeneous coords):")
    for i, move in enumerate(game.history):
        print(f"  Piece {i}: MB_curr = {move['MB_curr']} (homogeneous)")
    
    matrix = WeightedMatrixABC(game, sigma=1.0)
    
    # Extract and convert to homogeneous coordinates
    positions = matrix.compute_piece_positions()
    print("\nConverted to homogeneous coordinates for matrix computations:")
    print(positions)
    
    # Distance matrix
    D = matrix.compute_distance_matrix()
    print("\n1. DISTANCE MATRIX D[i,j] (2D distance only):")
    print(D.round(3))
    print(f"\nD[0,1] = {D[0,1]:.3f} ∈ [{game.t}, {game.T}]? {game.t <= D[0,1] <= game.T} ✓")
    
    # Adjacency matrix
    A = matrix.compute_adjacency_matrix()
    print("\n2. ADJACENCY MATRIX A[i,j]:")
    print(A.astype(int))
    
    # Spatial matrix
    S = matrix.compute_spatial_matrix()
    print("\n3. SPATIAL MATRIX S[i,j] (Gaussian):")
    print(S.round(3))
    
    # Feature matrix
    F = matrix.compute_feature_matrix()
    print("\n4. FEATURE MATRIX F[i,j] (cosine similarity):")
    print(F.round(3))
    
    # Weighted matrix
    W = matrix.compute_weighted_matrix()
    print("\n5. WEIGHTED MATRIX W[i,j] = A ⊙ S ⊙ F:")
    print(W.round(3))
    print(f"\nW[0,1] = {A[0,1]:.0f} × {S[0,1]:.3f} × {F[0,1]:.3f} = {W[0,1]:.3f} ✓")
    
    print("\n" + "="*80)


# ============================================================================
# EXAMPLE 2: Three Pieces with Isolation
# ============================================================================

def example_2():
    """Three pieces with mixed adjacency."""
    print("\n" + "="*80)
    print("EXAMPLE 2: Three Pieces with Isolation")
    print("="*80)
    
    game = ABCModelDynamic(n=2, t=1.0, T=1.41)
    
    game.add_piece((0.0, 0.0), (1.0, 1.0, 1.0),
                   kappa_vector=np.array([1.0, 1.0, 1.0]))
    
    game.add_piece((1.0, 0.0), (1.0, 1.0, 1.0),
                   kappa_vector=np.array([1.0, 1.0, 1.0]))
    
    game.add_piece((3.0, 3.0), (1.0, 1.1, 1.0),
                   kappa_vector=np.array([1.0, 1.1, 1.0]))
    
    print("\nPiece Positions (from abc_model.py storage):")
    for i, pos in enumerate(game.piece_positions):
        mb = game.history[i]['MB_curr']
        print(f"  Piece {i}: stored={pos}, MB={mb}")
    
    matrix = WeightedMatrixABC(game, sigma=1.0)
    
    # Distance matrix
    D = matrix.compute_distance_matrix()
    print("\n1. DISTANCE MATRIX D[i,j]:")
    print(D.round(3))
    print(f"\nD[0,1] = {D[0,1]:.3f} → adjacent ✓")
    print(f"D[0,2] = {D[0,2]:.3f} → isolated")
    print(f"D[1,2] = {D[1,2]:.3f} → isolated")
    
    # Adjacency matrix
    A = matrix.compute_adjacency_matrix()
    print("\n2. ADJACENCY MATRIX A[i,j]:")
    print(A.astype(int))
    print(f"\nA[0,1] = {int(A[0,1])} (adjacent) ✓")
    print(f"A[2,2] = {int(A[2,2])} (isolated piece!) ✓")
    
    # Spatial matrix
    S = matrix.compute_spatial_matrix()
    print("\n3. SPATIAL MATRIX S[i,j]:")
    print(S.round(3))
    
    # Feature matrix
    F = matrix.compute_feature_matrix()
    print("\n4. FEATURE MATRIX F[i,j]:")
    print(F.round(3))
    
    # Weighted matrix
    W = matrix.compute_weighted_matrix()
    print("\n5. WEIGHTED MATRIX W[i,j] = A ⊙ S ⊙ F:")
    print(W.round(6))
    print(f"\nW[0,1] = {A[0,1]:.0f} × {S[0,1]:.3f} × {F[0,1]:.3f} = {W[0,1]:.3f} ✓")
    print(f"W[2,2] = {A[2,2]:.0f} × {S[2,2]:.3f} × {F[2,2]:.3f} = {W[2,2]:.3f} ✓")
    print(f"W[0,2] = {A[0,2]:.0f} × {S[0,2]:.6f} × {F[0,2]:.3f} = {W[0,2]:.6f} ✓")
    
    print("\n" + "="*80)


if __name__ == "__main__":
    print("\n" + "="*80)
    print("WEIGHTED MATRIX IMPLEMENTATION (TRULY FINAL CORRECTED)")
    print("Section 6: W[i,j](t) = A[i,j] ⊙ S[i,j] ⊙ F[i,j]")
    print("Aligned with abc_model.py: Converts (x,y) to [x,y,1] homogeneous coords")
    print("="*80)
    
    example_1()
    example_2()
    
    print("\n" + "="*80)
    print("KEY ALIGNMENT WITH abc_model.py")
    print("="*80)
    print("""
abc_model.py structure:
    - piece_positions: stored as (x, y) tuples [2D board representation]
    - MB_current: calculated as a_product @ c0 = [x, y, 1] [homogeneous]
    - These represent the SAME location on the board

matrix_model.py now:
    - Reads piece_positions as (x, y) from abc_model
    - Converts to [x, y, 1] homogeneous coordinates
    - Computes distances using 2D components [x, y]
    - Applies matrix algebra correctly

Result:
    - Pieces treated as [x, y, 1] internally
    - Positions (1.0, 0.0) → [1.0, 0.0, 1.0]
    - Positions (1.0, 1.0) → [1.0, 1.0, 1.0]
    - Matrix dimensions: num_pieces × num_pieces
    - Distance uses only 2D components
    - Full algebraic alignment with abc_model.py
""")
    print("="*80 + "\n")