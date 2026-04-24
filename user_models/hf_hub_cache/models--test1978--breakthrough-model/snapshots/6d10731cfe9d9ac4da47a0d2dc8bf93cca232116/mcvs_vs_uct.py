"""
BREAKTHROUGH MCVS vs UCT TOURNAMENT - 200 GAMES WITH DETAILED LOGGING

Exact analogue of the chess tournament script.
- Zone database saved AFTER EVERY GAME
- Detailed per-game training logging (examples, time taken)
- Shows progressive learning in console and learning_log.txt
"""

import torch
import numpy as np
import os
import time
import threading
import queue
import random
from datetime import datetime
from breakthrough_mcvs import (
    Breakthrough,
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
        """Open log file for writing"""
        self.file_handle = open(self.filepath, 'w', encoding='utf-8')
        self._write_header()
    
    def _write_header(self):
        """Write log file header"""
        self.file_handle.write("=" * 100 + "\n")
        self.file_handle.write("BREAKTHROUGH TOURNAMENT - MOVE GENERATION LOG\n")
        self.file_handle.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.file_handle.write("=" * 100 + "\n\n")
        self.file_handle.flush()
    
    def log_move(self, game_num, move_num, player_name, agent_type, 
                 simulations, time_taken, move, board_state=""):
        """Log a single move with detailed statistics"""
        msg = (
            f"[Game {game_num:3d}] [Move {move_num:3d}] "
            f"Player: {player_name:4s} ({agent_type:4s}) | "
            f"Sims: {simulations:5d} | "
            f"Time: {time_taken:6.3f}s | "
            f"Move: {move}\n"
        )
        self.file_handle.write(msg)
        self.file_handle.flush()
    
    def log_game_summary(self, game_num, winner, total_moves, mcvs_stats, uct_stats):
        """Log summary statistics for a completed game"""
        self.file_handle.write("\n" + "-" * 100 + "\n")
        self.file_handle.write(f"GAME {game_num} SUMMARY - Winner: {winner}\n")
        self.file_handle.write(f"Total moves: {total_moves}\n\n")
        
        self.file_handle.write(f"MCVS Statistics:\n")
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
        """Close log file"""
        if self.file_handle:
            self.file_handle.write("\n" + "=" * 100 + "\n")
            self.file_handle.write(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self.file_handle.write("=" * 100 + "\n")
            self.file_handle.close()

class LearningLogger:
    """Logs training phase timing and statistics to learning_log.txt"""
    
    def __init__(self, filepath="learning_log.txt"):
        self.filepath = filepath
        self.file_handle = None
        
    def open(self):
        """Open log file for writing"""
        self.file_handle = open(self.filepath, 'w', encoding='utf-8')
        self._write_header()
    
    def _write_header(self):
        """Write log file header"""
        self.file_handle.write("=" * 100 + "\n")
        self.file_handle.write("BREAKTHROUGH TOURNAMENT - LEARNING PHASE LOG\n")
        self.file_handle.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self.file_handle.write("=" * 100 + "\n\n")
        self.file_handle.flush()
    
    def log_training_queued(self, game_num, num_examples):
        msg = f"→ Queued training data from game {game_num} ({num_examples} examples)\n"
        self.file_handle.write(msg)
        self.file_handle.flush()

    def log_training_completed(self, game_num, num_examples, time_taken):
        msg = f"✓ Completed training on game {game_num} data ({num_examples} examples) in {time_taken:.2f}s\n"
        self.file_handle.write(msg)
        self.file_handle.flush()
    
    def log_initial_training(self, game_num, num_examples, time_taken, epochs):
        """Log initial synchronous training after game 1"""
        msg = (
            f"\n{'=' * 100}\n"
            f"INITIAL TRAINING (Game {game_num})\n"
            f"{'=' * 100}\n"
            f"Training examples: {num_examples}\n"
            f"Epochs: {epochs}\n"
            f"Training time: {time_taken:.2f}s\n"
            f"Zone guidance enabled: λ = 1.0\n"
            f"{'=' * 100}\n\n"
        )
        self.file_handle.write(msg)
        self.file_handle.flush()
    
    def log_tournament_summary(self, total_games_learned, total_training_time, avg_time_per_game):
        msg = (
            f"\n{'=' * 100}\n"
            f"FINAL TRAINING SUMMARY\n"
            f"{'=' * 100}\n"
            f"Games learned from: {total_games_learned}\n"
            f"Total training time: {total_training_time/60:.1f} minutes\n"
            f"Avg time per game: {avg_time_per_game:.1f}s\n"
            f"{'=' * 100}\n"
        )
        self.file_handle.write(msg)
        self.file_handle.flush()
    
    def close(self):
        if self.file_handle:
            self.file_handle.close()

class TrainingManager:
    def __init__(self, policy_net, value_net, device, learning_logger):
        self.policy_net = policy_net
        self.value_net = value_net
        self.device = device
        self.learning_logger = learning_logger
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.stop_event = threading.Event()
        self.total_training_time = 0.0
        self.games_processed = 0
        self.thread.start()

    def queue_training(self, W_states, policies, values, game_num):
        self.queue.put((W_states, policies, values, game_num))
        self.learning_logger.log_training_queued(game_num, len(W_states))

    def _worker(self):
        while not self.stop_event.is_set():
            try:
                W_states, policies, values, game_num = self.queue.get(timeout=1.0)
                start = time.time()
                train_networks(self.policy_net, self.value_net, W_states, policies, values, epochs=10, device=self.device)
                elapsed = time.time() - start
                self.total_training_time += elapsed
                self.games_processed += 1
                self.learning_logger.log_training_completed(game_num, len(W_states), elapsed)
                self.queue.task_done()
            except queue.Empty:
                pass

    def stop(self):
        self.stop_event.set()
        self.thread.join()

class BreakthroughTournament:
    def __init__(self, num_games=200, time_per_move=1.0, max_moves_per_game=200, device='cpu', 
                 output_file="breakthrough_tournament_results.txt", ablation_no_nets=False):
        self.num_games = num_games
        self.time_per_move = time_per_move
        self.max_moves_per_game = max_moves_per_game
        self.device = device
        self.output_file = output_file

        self.move_logger = DetailedMoveLogger()
        self.learning_logger = LearningLogger()
        self.report_lines = []

        self.move_logger.open()
        self.learning_logger.open()

        self.ablation_no_nets = ablation_no_nets

        self.zone_db = HilbertOrderedZoneDatabase("breakthrough_zone_db.npz")
        self.policy_net = PolicyNetworkCNN().to(device)
        self.value_net = ValueNetworkCNN().to(device)

        self.agent_mcvs = MCVSSearcher(self.policy_net, self.value_net, self.zone_db, device=device, lambda_zone=0.0)
        self.agent_uct = UCTSearcher()

        self.training_manager = None
        self.networks_initialized = False

        self.results = {
            "MCVS": {"wins": 0, "as_white": 0, "as_black": 0, "moves": []},
            "UCT": {"wins": 0, "as_white": 0, "as_black": 0, "moves": []},
            "Draw": {"count": 0, "moves": []}
        }

        if self.ablation_no_nets:
            print("ABLATION MODE: Zone guidance only (NO neural networks)")
            self.agent_mcvs = MCVSSearcher(
                policy_net=None, value_net=None, zone_db=self.zone_db,
                device=device, lambda_zone=1.0, use_nets=False
            )
            self.networks_initialized = False
        else:
            self.policy_net = PolicyNetworkCNN().to(device)
            self.value_net = ValueNetworkCNN().to(device)
            self.agent_mcvs = MCVSSearcher(
                self.policy_net, self.value_net, self.zone_db,
                device=device, lambda_zone=0.0, use_nets=True
            )

    def log_line(self, text):
        print(text)
        self.report_lines.append(text)

    def extract_training_and_zone_data(self, moves_made, pi_records, outcome):
        game = Breakthrough()
        W_states = []
        policies = []
        values = []

        for step, move in enumerate(moves_made):
            # Current state matrix
            W = game.get_weighted_adjacency_matrix().copy()
            W_states.append(W)

            # Policy target
            pi = np.zeros(4096, dtype=np.float32)  # 64 from-squares * 64 to-squares

            visits = pi_records[step]  # dict move_tuple -> count, or None if UCT move

            legal_moves = game.get_legal_moves()

            if visits is not None and sum(visits.values()) > 0:
                total = sum(visits.values())
                for m, count in visits.items():
                    fr, fc, tr, tc = m
                    from_sq = fr * 8 + fc
                    to_sq = tr * 8 + tc
                    idx = from_sq * 64 + to_sq
                    pi[idx] = count / total
            else:
                # Uniform over legal moves
                if legal_moves:
                    p = 1.0 / len(legal_moves)
                    for m in legal_moves:
                        fr, fc, tr, tc = m
                        from_sq = fr * 8 + fc
                        to_sq = tr * 8 + tc
                        idx = from_sq * 64 + to_sq
                        pi[idx] = p

            policies.append(pi)

            # Value target (from perspective of player to move at this position)
            if outcome is None:  # Draw
                v = 0.0
            elif outcome == Breakthrough.PLAYER1:
                # PLAYER1 wins → +1 if PLAYER1 to move here, -1 if PLAYER2 to move
                v = 1.0 if (step % 2 == 0) else -1.0
            else:  # PLAYER2 wins
                v = 1.0 if (step % 2 == 1) else -1.0

            values.append(v)

            # Apply the move to advance the replay
            game.apply_move(move)

        return W_states, policies, values

    def play_game(self, agent_white, agent_black, game_idx):
        game = Breakthrough()
        moves = 0
        moves_made = []
        pi_records = []
        mcvs_stats = {"total_sims": 0, "total_time": 0.0, "moves": 0}
        uct_stats = {"total_sims": 0, "total_time": 0.0, "moves": 0}

        while not game.is_terminal() and moves < self.max_moves_per_game:
            moves += 1
            current_player = Breakthrough.PLAYER1 if moves % 2 == 1 else Breakthrough.PLAYER2
            searcher = agent_white if current_player == Breakthrough.PLAYER1 else agent_black
            agent_type = "MCVS" if searcher is self.agent_mcvs else "UCT"

            if agent_type == "MCVS":
                mcvs_stats["moves"] += 1
            else:
                uct_stats["moves"] += 1

            start_t = time.time()
            output = searcher.search_with_time_budget(game.copy(), self.time_per_move)
            # Robust handling for different return types
            if isinstance(output, tuple) or isinstance(output, list):
                visits_dict = output[0]
                sims = output[1] if len(output) > 1 else sum(output[0].values()) if output[0] else 0
            elif isinstance(output, dict):
                visits_dict = output
                sims = sum(output.values()) if output else 0
            else:
                raise ValueError(f"Unexpected search return: {type(output)}")
            t_taken = time.time() - start_t

            if agent_type == "MCVS":
                mcvs_stats["total_sims"] += sims
                mcvs_stats["total_time"] += t_taken
            else:
                uct_stats["total_sims"] += sims
                uct_stats["total_time"] += t_taken

            chosen_move = max(visits_dict, key=visits_dict.get) if visits_dict else random.choice(game.get_legal_moves())

            player_name = "Player1" if current_player == Breakthrough.PLAYER1 else "Player2"
            self.move_logger.log_move(game_idx, moves, player_name, agent_type, sims, t_taken, str(chosen_move))

            game.apply_move(chosen_move)
            moves_made.append(chosen_move)
            pi_records.append(visits_dict if agent_type == "MCVS" else None)

        # Averages
        if mcvs_stats["moves"] > 0:
            mcvs_stats["avg_sims"] = mcvs_stats["total_sims"] / mcvs_stats["moves"]
            mcvs_stats["avg_time"] = mcvs_stats["total_time"] / mcvs_stats["moves"]
        else:
            mcvs_stats["avg_sims"] = 0.0
            mcvs_stats["avg_time"] = 0.0

        if uct_stats["moves"] > 0:
            uct_stats["avg_sims"] = uct_stats["total_sims"] / uct_stats["moves"]
            uct_stats["avg_time"] = uct_stats["total_time"] / uct_stats["moves"]
        else:
            uct_stats["avg_sims"] = 0.0
            uct_stats["avg_time"] = 0.0

        # Winner
        if moves >= self.max_moves_per_game:
            winner = "Draw"
            winner_val = 0
            self.move_logger.log_game_summary(game_idx, "Draw (move limit)", moves, mcvs_stats, uct_stats)
        else:
            winner_val = game.check_winner()
            if winner_val == 0:
                winner = "Draw"
            elif winner_val == Breakthrough.PLAYER1:
                winner = "MCVS" if agent_white is self.agent_mcvs else "UCT"
            else:
                winner = "MCVS" if agent_black is self.agent_mcvs else "UCT"

            self.move_logger.log_game_summary(game_idx, winner, moves, mcvs_stats, uct_stats)

        return winner, moves, moves_made, pi_records, winner_val

    def run_tournament(self):
        start_time = time.time()
        self.log_line("Starting 200-game Breakthrough MCVS vs UCT tournament")

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

            # Sample positions for zone DB (skip if no states were recorded)
            if not W_states:
                selected = []
            else:
                selected = [0]
                if len(W_states) > 1:
                    selected.append(len(W_states)-1)
                    for i in range(1, len(W_states)-1):
                        if random.random() < 0.3:
                            selected.append(i)

            # Label stored matrices according to the game's global outcome (not per-position training value)
            if outcome == Breakthrough.PLAYER1:
                label = 1
            elif outcome == Breakthrough.PLAYER2:
                label = -1
            else:
                label = 0

            for i in selected:
                W = W_states[i]
                if label > 0:
                    self.zone_db.add_winning_matrix(W)
                elif label < 0:
                    self.zone_db.add_losing_matrix(W)
                else:
                    self.zone_db.add_draw_matrix(W)

            # SAVE ZONE DB AFTER EVERY GAME
            self.zone_db.save()

            if game_idx == 1 and not self.ablation_no_nets:
                # Initial synchronous training after game 1 (full system only)
                train_start = time.time()
                train_networks(
                    self.policy_net, self.value_net,
                    W_states, policies, values,
                    epochs=10,  # More epochs for bootstrap
                    device=self.device
                )
                train_time = time.time() - train_start
                self.learning_logger.log_initial_training(game_idx, len(W_states), train_time, 10)
                self.agent_mcvs.lambda_zone = 1.0  # Turn on zone guidance after initial training
                self.networks_initialized = True
                self.training_manager = TrainingManager(
                    self.policy_net, self.value_net, self.device, self.learning_logger
                )
                print(f"Initial training completed in {train_time:.2f}s. Zone guidance enabled.")

            if not self.ablation_no_nets and game_idx > 1:
                # Queue asynchronous training for subsequent games (full system only)
                self.training_manager.queue_training(W_states, policies, values, game_idx)

            # Progress logging
            elapsed = time.time() - start_time
            remaining = elapsed / game_idx * (self.num_games - game_idx)
            queue_size = self.training_manager.queue.qsize() if self.networks_initialized else 0
            learned_games = self.training_manager.games_processed if self.networks_initialized else 0
            db_size = len(self.zone_db.winning_matrices) + len(self.zone_db.losing_matrices) + len(self.zone_db.draw_matrices)
            self.log_line(
                f"Game {game_idx:3d}/{self.num_games} | {winner:10s} (moves={moves:3d}) | "
                f"MCVS wins: {self.results['MCVS']['wins']:3d} | Draws: {self.results['Draw']['count']:3d} | "
                f"Queue: {queue_size} | Learned: {learned_games} | DB: {db_size} | ETA: {remaining/60:.1f} min"
            )

        if self.networks_initialized:
            self.log_line("\nWaiting for remaining training to finish...")
            self.training_manager.stop()

            avg_training_time = (self.training_manager.total_training_time / 
                                 self.training_manager.games_processed 
                                 if self.training_manager.games_processed > 0 else 0)
            self.learning_logger.log_tournament_summary(
                self.training_manager.games_processed,
                self.training_manager.total_training_time,
                avg_training_time
            )
            self.log_line("All training completed.")

        # Final summary
        self.log_line("\n" + "=" * 80)
        self.log_line("TOURNAMENT SUMMARY")
        self.log_line("=" * 80)
        
        mcvs_wins = self.results["MCVS"]["wins"]
        uct_wins = self.results["UCT"]["wins"]
        draws = self.results["Draw"]["count"]
        
        self.log_line(f"\nMCVS (λ-PUCT): {mcvs_wins} wins")
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
        
        if self.networks_initialized:
            total_db = len(self.zone_db.winning_matrices) + len(self.zone_db.losing_matrices) + len(self.zone_db.draw_matrices)
            self.log_line(f"\nZone Database Statistics:")
            self.log_line(f"  Winning positions: {len(self.zone_db.winning_matrices)}")
            self.log_line(f"  Losing positions: {len(self.zone_db.losing_matrices)}")
            self.log_line(f"  Draw positions: {len(self.zone_db.draw_matrices)}")
            self.log_line(f"  Total positions: {total_db:,}")
            self.log_line(f"  Games learned from: {self.training_manager.games_processed}")
            self.log_line(f"  Total training time: {self.training_manager.total_training_time/60:.1f} min")

        total_time = time.time() - start_time
        self.log_line("\n" + "=" * 80)
        self.log_line(f"Tournament Duration: {total_time/60:.1f} minutes")
        self.log_line(f"Avg time per game: {total_time/self.num_games:.1f}s")
        self.log_line("=" * 80 + "\n")

        with open(self.output_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.report_lines))

        self.move_logger.close()
        self.learning_logger.close()

if __name__ == "__main__":
    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    print(f"Using device: {DEVICE}")

    # 2. Ablation: Zone-only (no nets)
    print("\n=== RUNNING ABLATION: Zone guidance ONLY (no neural networks) ===")
    tournament_ablation = BreakthroughTournament(
        num_games=100, time_per_move=0.5, max_moves_per_game=100,
        device=DEVICE, output_file="breakthrough_zone_only_results.txt",
        ablation_no_nets=True  # Set to True to disable neural networks entirely
    )
    tournament_ablation.run_tournament()