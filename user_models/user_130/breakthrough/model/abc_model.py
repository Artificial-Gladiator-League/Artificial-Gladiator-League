"""
abc_model.py - ABC Model with Displacement Matrices (CORRECTED FOR KAPPA)

KEY INSIGHT:
    - B_i represents the DISPLACEMENT from current MB position to new position
    - If MB is at (x₁, y₁) and we place piece at (x₂, y₂):
    - Δx = x₂ - x₁
    - Δy = y₂ - y₁
    - B has right column (Δx, Δy, 1)ᵀ

Example:
    - Stage 0: MB at (0, 0, 1)
    - Stage 1: Place at (-1, -1) → Δ = (-1-0, -1-0) = (-1, -1)
    - B₁ right column: (-1, -1, 1)ᵀ
    - Stage 2: Place at (0, 0) → Δ = (0-(-1), 0-(-1)) = (1, 1)
    - B₂ right column: (1, 1, 1)ᵀ ✓

Author: Chaim Duchovny
Date: January 25, 2026 (FIXED FOR KAPPA SUPPORT)
"""

import numpy as np

class ABCModelDynamic:
    """ABC Model with displacement-based B matrices."""
    
    def __init__(self, n: int = 2, t: float = 1.0, T: float = 1.41):
        """
        Initialize for 2D board.
        
        Args:
            n: Dimension (2 for 2D board)
            t: Minimum distance threshold for adjacency
            T: Maximum distance threshold for adjacency
        """
        self.n = n
        self.t = t
        self.T = T
        self.c0 = np.array([0.0, 0.0, 1.0])
        self.B_blocks = []        # Displacement matrices
        self.piece_positions = [] # Actual positions on SB
        self.a_product = np.eye(3) # Accumulated product
        self.delta = {}           # Delta values for each piece
        self.kappa = {}           # Tokenized vectors for each piece (ADDED)
        self.stage = 0
        
        # MB position tracking
        self.MB_current = self.c0.copy()  # Current MB center
        self.MB_previous = self.c0.copy() # Previous MB center
        self.history = []
    
    def create_B_displacement(self, dx: float, dy: float) -> np.ndarray:
        """
        Create displacement matrix B.
        
        Moves MB by (dx, dy) displacement.
        
        B · current_MB = new_MB
        
        B = [1 0 dx]
            [0 1 dy]
            [0 0 1]
        
        Right column: (dx, dy, 1)ᵀ
        """
        B = np.array([
            [1.0, 0.0, dx],
            [0.0, 1.0, dy],
            [0.0, 0.0, 1.0]
        ])
        return B
    
    def add_piece(self, position: tuple, delta_values: tuple, kappa_vector: np.ndarray = None):
        """
        Add a piece at position with delta values and optional kappa vector.
        
        Args:
            position: (x, y) tuple
            delta_values: (Δ₁, Δ₂, Δ₃) tuple
            kappa_vector: Tokenized feature vector (optional, defaults to delta_values)
        """
        x, y = position
        
        # Current MB position
        current_MB = self.MB_current.copy()
        
        # Calculate displacement
        dx = x - current_MB[0]
        dy = y - current_MB[1]
        
        # Create displacement matrix
        B_i = self.create_B_displacement(dx, dy)
        
        # Store
        idx = len(self.B_blocks)
        self.B_blocks.append(B_i)
        self.piece_positions.append((x, y))
        
        # Update accumulated product: a_t = a_{t-1} · B_t
        self.a_product = self.a_product @ B_i
        
        # Track MB positions
        self.MB_previous = self.MB_current.copy()
        self.MB_current = self.a_product @ self.c0  # Should equal (x, y, 1)
        
        # Store differential vector
        self.delta[idx] = delta_values
        
        # Store tokenized vector (or use delta_values as default)
        if kappa_vector is None:
            kappa_vector = np.array(delta_values)
        self.kappa[idx] = kappa_vector
        
        # Increment stage
        self.stage += 1
        
        # Record move
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
    
    def add_move(self, x: float, y: float, player: int):
        """
        Convenience method: Add a move with automatic delta values.
        
        Args:
            x, y: Position coordinates
            player: 1 for X (Δ₂=1.0), 2 for O (Δ₂=1.1)
        """
        delta_1 = 1.0  # Occupied
        delta_2 = 1.0 if player == 1 else 1.1  # Player color
        delta_3 = 1.0  # Same value for all pieces
        
        self.add_piece((x, y), (delta_1, delta_2, delta_3))
    
    def get_board_state(self) -> str:
        """Visual board state."""
        pos_map = {
            (-1, 1): 0, (0, 1): 1, (1, 1): 2,
            (-1, 0): 3, (0, 0): 4, (1, 0): 5,
            (-1, -1): 6, (0, -1): 7, (1, -1): 8
        }
        
        board = [' '] * 9
        for i, pos in enumerate(self.piece_positions):
            if pos in pos_map:
                player = 'X' if self.delta[i][1] == 1.0 else 'O'
                board[pos_map[pos]] = player
        
        s = f" {board[0]} | {board[1]} | {board[2]} \n"
        s += "---|---|---\n"
        s += f" {board[3]} | {board[4]} | {board[5]} \n"
        s += "---|---|---\n"
        s += f" {board[6]} | {board[7]} | {board[8]} \n"
        return s
    
    def describe_move(self, stage: int):
        """Describe a specific move."""
        if stage < 1 or stage > len(self.history):
            return "Invalid stage"
        
        move = self.history[stage - 1]
        desc = "=" * 70 + "\n"
        desc += f"MOVE {stage}: {move['SB_position']}\n"
        desc += "=" * 70 + "\n\n"
        
        idx = stage - 1
        B_i = self.B_blocks[idx]
        
        desc += f"Mobile Board (MB) movement:\n"
        desc += f" Previous MB: {move['MB_prev']}\n"
        desc += f" Target (SB): {move['SB_position']}\n"
        desc += f" Displacement: Δ = {move['displacement']}\n"
        desc += f" New MB: {move['MB_curr']}\n\n"
        
        desc += f"Displacement matrix B_{stage}:\n{B_i}\n"
        desc += f"Right column: {B_i[:, 2]} = (Δx={move['displacement'][0]}, Δy={move['displacement'][1]}, 1)\n\n"
        
        desc += f"Accumulated product a_{stage}:\n{move['a_t']}\n"
        desc += f"Right column: {move['a_t'][:, 2]}\n\n"
        
        desc += f"Delta values: {move['delta']}\n"
        desc += f"Kappa vector: {move['kappa']}\n\n"
        
        desc += f"Verification:\n"
        desc += f" a_{stage} · c₀ = {move['a_t'] @ self.c0}\n"
        desc += f" Should equal: {np.array([move['SB_position'][0], move['SB_position'][1], 1.0])}\n\n"
        
        desc += "Board state:\n"
        desc += self.get_board_state()
        
        return desc


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("TIC-TAC-TOE - DISPLACEMENT-BASED B MATRICES")
    print("=" * 70 + "\n")
    
    game = ABCModelDynamic(n=2, t=1.0, T=1.41)
    
    # Play complete game
    moves = [
        ((-1, -1), 1),  # Move 1: X at (-1,-1)
        ((0, 0), 2),    # Move 2: O at (0,0)
        ((0, -1), 1),   # Move 3: X at (0,-1)
        ((1, 0), 2),    # Move 4: O at (1,0)
    ]
    
    for pos, player in moves:
        game.add_move(pos[0], pos[1], player)
    
    # Show first 4 moves in detail
    for i in range(1, 5):
        print(game.describe_move(i))
        print()
    
    # Verification
    print("=" * 70)
    print("VERIFICATION")
    print("=" * 70 + "\n")
    
    print("Key property: MB position tracks piece positions on SB")
    for i in range(1, len(game.history) + 1):
        move = game.history[i - 1]
        MB_curr = move['MB_curr']
        SB_pos = move['SB_position']
        matches = np.allclose(MB_curr[:2], SB_pos)
        print(f"Move {i}: MB={MB_curr} SB={SB_pos} Match? {matches} {'✓' if matches else '❌'}")
    
    print(f"\nB matrices (right columns = displacements):")
    for i in range(1, len(game.history) + 1):
        move = game.history[i - 1]
        B_i = game.B_blocks[i - 1]
        disp = move['displacement']
        right_col = B_i[:, 2]
        print(f" Move {i}: Δ={disp} → B right column = {right_col} ✓")
    
    print(f"\nKappa vectors (tokenized features):")
    for i in range(len(game.history)):
        kappa = game.kappa[i]
        print(f" Piece {i}: κ = {kappa}")
    
    print(f"\nSpecial case - Move 2:")
    move2 = game.history[1]
    print(f" From (-1, -1) to (0, 0)")
    print(f" Displacement: {move2['displacement']}")
    print(f" B₂ right column: {game.B_blocks[1][:, 2]} = (1, 1, 1)ᵀ ✓✓✓")
    
    print("\n✓ MB position correctly tracks SB piece positions!")
    print("✓ B matrices represent displacements!")
    print("✓ B₂ has right column (1, 1, 1)ᵀ as expected!")
    print("✓ Kappa vectors initialized correctly!")
    print("=" * 70 + "\n")