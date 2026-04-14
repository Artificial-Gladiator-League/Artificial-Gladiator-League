import torch
import chess
import numpy as np
import os
import time
import threading
import queue
import random
from datetime import datetime
from chess_mcvs import (
    Chess,
    PolicyNetworkCNN,
    ValueNetworkCNN,
    HilbertOrderedZoneDatabase,
    MCVSSearcher,
    UCTSearcher,
    train_networks
)

class DetailedMoveLogger:
    """Logs detailed move generation statistics to move_log.txt"""
    
    def __init__(self, filepath="move_log.txt"):
        self.filepath = filepath
        self.file_handle = None
        
    def open(self):
        self.file_handle = open(self.filepath, 'w', encoding='utf-8')
        self._write_header()
    
    def _write_header(self):
        self.file_handle.write("=" * 100 + "\n")
        self.file_handle.write("CHESS TOURNAMENT - MOVE GENERATION LOG (ZONE-ONLY ABLATION)\n")
        self.file_handle.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.file_handle.write("=" * 100 + "\n\n")
        self.file_handle.flush()
    
    def log_move(self, game_num, move_num, player_name, agent_type, 
                 simulations, time_taken, move):
        msg = (
            f"[Game {game_num:3d}] [Move {move_num:3d}] "
            f"Player: {player_name:5s} ({agent_type:4s}) | "
            f"Sims: {simulations:5d} | "
            f"Time: {time_taken:6.3f}s | "
            f"Move: {move}\n"
        )
        self.file_handle.write(msg)
        self.file_handle.flush()
    
    def log_game_summary(self, game_num, winner, total_moves, mcvs_stats, uct_stats):
        self.file_handle.write("\n" + "-" * 100 + "\n")
        self.file_handle.write(f"GAME {game_num} SUMMARY - Winner: {winner}\n")
        self.file_handle.write(f"Total moves: {total_moves}\n\n")
        
        self.file_handle.write(f"MCVS (Zone-only) Statistics:\n")
        self.file_handle.write(f"  Total simulations: {mcvs_stats['total_sims']:,}\n")
        self.file_handle.write(f"  Avg sims/move: {mcvs_stats['avg_sims']:.1f}\n")
        self.file_handle.write(f"  Total time: {mcvs_stats['total_time']:.2f}s\n")
        self.file_handle.write(f"  Avg time/move: {mcvs_stats['avg_time']:.3f}s\n\n")
        
        self.file_handle.write(f"UCT Statistics:\n")
        self.file_handle.write(f"  Total simulations: {uct_stats['total_sims']:,}\n")
        self.file_handle.write(f"  Avg sims/move: {uct_stats['avg_sims']:.1f}\n")
        self.file_handle.write(f"  Total time: {uct_stats['total_time']:.2f}s\n")
        self.file_handle.write(f"  Avg time/move: {uct_stats['avg_time']:.3f}s\n")
        self.file_handle.write("-" * 100 + "\n\n")
        self.file_handle.flush()
    
    def close(self):
        if self.file_handle:
            self.file_handle.write("\n" + "=" * 100 + "\n")
            self.file_handle.write(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self.file_handle.write("=" * 100 + "\n")
            self.file_handle.close()

class ChessTournament:
    def __init__(self, num_games=200, time_per_move=3.0, max_moves_per_game=200, device='cpu', 
                 output_file="chess_zone_only_results.txt", ablation_no_nets=True):
        self.num_games = num_games
        self.time_per_move = time_per_move
        self.max_moves_per_game = max_moves_per_game
        self.device = device
        self.output_file = output_file
        self.ablation_no_nets = ablation_no_nets

        self.move_logger = DetailedMoveLogger()
        self.move_logger.open()

        self.zone_db = HilbertOrderedZoneDatabase("chess_zone_db.npz")

        # ABLATION: Zone-only (no networks)
        self.agent_mcvs = MCVSSearcher(
            policy_net=None, value_net=None, zone_db=self.zone_db,
            device=device, lambda_zone=1.0, use_nets=False
        )
        self.agent_uct = UCTSearcher()

        self.report_lines = []

        self.results = {
            "MCVS": {"wins": 0, "as_white": 0, "as_black": 0, "moves": []},
            "UCT": {"wins": 0, "as_white": 0, "as_black": 0, "moves": []},
            "Draw": {"count": 0, "moves": []}
        }

    def log_line(self, text):
        print(text)
        self.report_lines.append(text)

    def extract_training_and_zone_data(self, moves_made, pi_records, outcome):
        game = Chess()
        W_states = []
        policies = []
        values = []

        winner_color = outcome.winner if outcome and outcome.winner is not None else None

        for step, move in enumerate(moves_made):
            W = game.get_weighted_adjacency_matrix().copy()
            W_states.append(W)

            legal_moves = game.get_legal_moves()
            pi = np.zeros(4096, dtype=np.float32)

            visits = pi_records[step]
            if visits is not None and sum(visits.values()) > 0:
                total = sum(visits.values())
                for m, count in visits.items():
                    idx = m.from_square * 64 + m.to_square
                    pi[idx] = count / total
            else:
                if legal_moves:
                    p = 1.0 / len(legal_moves)
                    for m in legal_moves:
                        idx = m.from_square * 64 + m.to_square
                        pi[idx] = p

            policies.append(pi)

            if winner_color is None:
                v = 0.0
            else:
                ptm_white = (step % 2 == 0)
                ptm = chess.WHITE if ptm_white else chess.BLACK
                v = 1.0 if winner_color == ptm else -1.0
            values.append(v)

            game.apply_move(move)

        return W_states, policies, values

    def play_game(self, agent_white, agent_black, game_idx):
        game = Chess()
        moves = 0
        moves_made = []
        pi_records = []
        mcvs_stats = {"total_sims": 0, "total_time": 0.0, "moves": 0}
        uct_stats = {"total_sims": 0, "total_time": 0.0, "moves": 0}

        while not game.is_terminal() and moves < self.max_moves_per_game:
            moves += 1
            is_white = game.board.turn
            searcher = agent_white if is_white else agent_black
            agent_type = "MCVS" if searcher is self.agent_mcvs else "UCT"

            if agent_type == "MCVS":
                mcvs_stats["moves"] += 1
            else:
                uct_stats["moves"] += 1

            start_t = time.time()
            visits, sims = searcher.search_with_time_budget(game.copy(), self.time_per_move)
            t_taken = time.time() - start_t

            if agent_type == "MCVS":
                mcvs_stats["total_sims"] += sims
                mcvs_stats["total_time"] += t_taken
            else:
                uct_stats["total_sims"] += sims
                uct_stats["total_time"] += t_taken

            chosen_move = max(visits, key=visits.get) if visits else random.choice(game.get_legal_moves())

            player_name = "White" if is_white else "Black"
            self.move_logger.log_move(game_idx, moves, player_name, agent_type, sims, t_taken, chosen_move.uci())

            game.apply_move(chosen_move)
            moves_made.append(chosen_move)
            pi_records.append(visits if agent_type == "MCVS" else None)

        if mcvs_stats["moves"] > 0:
            mcvs_stats["avg_sims"] = mcvs_stats["total_sims"] / mcvs_stats["moves"]
            mcvs_stats["avg_time"] = mcvs_stats["total_time"] / mcvs_stats["moves"]
        if uct_stats["moves"] > 0:
            uct_stats["avg_sims"] = uct_stats["total_sims"] / uct_stats["moves"]
            uct_stats["avg_time"] = uct_stats["total_time"] / uct_stats["moves"]

        if moves >= self.max_moves_per_game:
            winner = "Draw"
            outcome = None
            self.move_logger.log_game_summary(game_idx, "Draw (move limit)", moves, mcvs_stats, uct_stats)
        else:
            outcome = game.board.outcome(claim_draw=True)
            if outcome is None or outcome.winner is None:
                winner = "Draw"
            elif outcome.winner == chess.WHITE:
                winner = "MCVS" if agent_white is self.agent_mcvs else "UCT"
            else:
                winner = "MCVS" if agent_black is self.agent_mcvs else "UCT"
            self.move_logger.log_game_summary(game_idx, winner, moves, mcvs_stats, uct_stats)

        return winner, moves, moves_made, pi_records, outcome

    def run_tournament(self):
        start_time = time.time()
        self.log_line("Starting 200-game Chess ablation tournament (zone guidance only vs UCT)")

        for game_idx in range(1, self.num_games + 1):
            if game_idx % 2 == 1:
                winner, moves, moves_made, pi_records, outcome = self.play_game(self.agent_mcvs, self.agent_uct, game_idx)
                white_name = "MCVS"
            else:
                winner, moves, moves_made, pi_records, outcome = self.play_game(self.agent_uct, self.agent_mcvs, game_idx)
                white_name = "UCT"

            if winner == "Draw":
                self.results["Draw"]["count"] += 1
                self.results["Draw"]["moves"].append(moves)
            else:
                self.results[winner]["wins"] += 1
                self.results[winner]["moves"].append(moves)
                if winner == white_name:
                    self.results[winner]["as_white"] += 1
                else:
                    self.results[winner]["as_black"] += 1

            W_states, policies, values = self.extract_training_and_zone_data(moves_made, pi_records, outcome)

            # Sample positions for zone DB (always, even in ablation)
            selected = [0]
            if len(W_states) > 1:
                selected.append(len(W_states)-1)
                for i in range(1, len(W_states)-1):
                    if random.random() < 0.3:
                        selected.append(i)

            # Label zone DB entries by the GAME-LEVEL ENGINE OUTCOME (which engine
            # won the game), NOT by the per-position training value. Training
            # targets (`values`) are still used for policy/value training, but
            # the zone DB should represent whether this position came from a
            # trajectory belonging to an engine win/loss/draw (see paper).
            old_w = len(self.zone_db.winning_matrices)
            old_l = len(self.zone_db.losing_matrices)
            old_d = len(self.zone_db.draw_matrices)

            for i in selected:
                W = W_states[i]
                if winner == "MCVS":
                    # This game's winner was MCVS → label these positions as winning
                    self.zone_db.add_winning_matrix(W)
                elif winner == "UCT":
                    # This game's winner was UCT → label these positions as losing
                    self.zone_db.add_losing_matrix(W)
                else:
                    # Draw
                    self.zone_db.add_draw_matrix(W)

            # Sanity check: ensure we added exactly `len(selected)` matrices to
            # the zone corresponding to the game-level outcome.
            new_w = len(self.zone_db.winning_matrices)
            new_l = len(self.zone_db.losing_matrices)
            new_d = len(self.zone_db.draw_matrices)
            if winner == "MCVS":
                assert new_w == old_w + len(selected), f"Zone DB labeling mismatch: expected {len(selected)} winning matrices added, got {new_w - old_w}"
            elif winner == "UCT":
                assert new_l == old_l + len(selected), f"Zone DB labeling mismatch: expected {len(selected)} losing matrices added, got {new_l - old_l}"
            else:
                assert new_d == old_d + len(selected), f"Zone DB labeling mismatch: expected {len(selected)} draw matrices added, got {new_d - old_d}"

            # SAVE ZONE DB AFTER EVERY GAME
            self.zone_db.save()

            # Progress logging
            elapsed = time.time() - start_time
            remaining = elapsed / game_idx * (self.num_games - game_idx)
            db_size = len(self.zone_db.winning_matrices) + len(self.zone_db.losing_matrices) + len(self.zone_db.draw_matrices)
            self.log_line(
                f"Game {game_idx:3d}/{self.num_games} | {winner:10s} (moves={moves:3d}) | "
                f"MCVS wins: {self.results['MCVS']['wins']:3d} | Draws: {self.results['Draw']['count']:3d} | "
                f"DB size: {db_size:,} | ETA: {remaining/60:.1f} min"
            )

        # Final summary
        self.log_line("\n" + "=" * 80)
        self.log_line("ABLATION TOURNAMENT SUMMARY (Zone-only vs UCT)")
        self.log_line("=" * 80)
        
        mcvs_wins = self.results["MCVS"]["wins"]
        uct_wins = self.results["UCT"]["wins"]
        draws = self.results["Draw"]["count"]
        
        self.log_line(f"\nMCVS (Zone-only λ-PUCT): {mcvs_wins} wins")
        self.log_line(f"UCT (Standard): {uct_wins} wins")
        self.log_line(f"Draws: {draws}")
        self.log_line(f"Total: {self.num_games}")
        
        if mcvs_wins + uct_wins > 0:
            win_rate = (mcvs_wins / (mcvs_wins + uct_wins)) * 100
            self.log_line(f"\nMCVS Win Rate (excluding draws): {win_rate:.1f}%")
        
        if self.results["MCVS"]["moves"]:
            avg_moves_mcvs = np.mean(self.results["MCVS"]["moves"])
            self.log_line(f"Avg moves (MCVS wins): {avg_moves_mcvs:.1f}")
        if self.results["UCT"]["moves"]:
            avg_moves_uct = np.mean(self.results["UCT"]["moves"])
            self.log_line(f"Avg moves (UCT wins): {avg_moves_uct:.1f}")
        
        self.log_line(f"\nMCVS White: {self.results['MCVS']['as_white']}, Black: {self.results['MCVS']['as_black']}")
        self.log_line(f"UCT White: {self.results['UCT']['as_white']}, Black: {self.results['UCT']['as_black']}")
        
        total_db = len(self.zone_db.winning_matrices) + len(self.zone_db.losing_matrices) + len(self.zone_db.draw_matrices)
        self.log_line(f"\nZone Database Statistics:")
        self.log_line(f"  Winning positions: {len(self.zone_db.winning_matrices)}")
        self.log_line(f"  Losing positions: {len(self.zone_db.losing_matrices)}")
        self.log_line(f"  Draw positions: {len(self.zone_db.draw_matrices)}")
        self.log_line(f"  Total positions: {total_db:,}")

        total_time = time.time() - start_time
        self.log_line("\n" + "=" * 80)
        self.log_line(f"Tournament Duration: {total_time/60:.1f} minutes")
        self.log_line(f"Avg time per game: {total_time/self.num_games:.1f}s")
        self.log_line("=" * 80 + "\n")

        with open(self.output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.report_lines))

        self.move_logger.close()

if __name__ == "__main__":
    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    print(f"Using device: {DEVICE}")

    print("\n=== RUNNING ABLATION: Zone guidance ONLY (no neural networks) ===")
    tournament_ablation = ChessTournament(
        num_games=100,
        time_per_move=1.0,
        max_moves_per_game=200,
        device=DEVICE,
        output_file="chess_zone_only_results.txt",
        ablation_no_nets=True
    )
    tournament_ablation.run_tournament()