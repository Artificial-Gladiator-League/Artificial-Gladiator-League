"""ShiRi / MCVS package tests. Run: python -m unittest apps.shiri.tests.test_shiri -v"""
import random
import time
import unittest

import chess
import numpy as np

from apps.games import breakthrough_engine as bt
from apps.shiri import ShiRi
from apps.shiri.abc import BoardEmbedding, TokenizedVector
from apps.shiri.constants import BLACK, DRAW, WHITE
from apps.shiri.database import AtomicRunner, GameTrace, VectorDatabase
from apps.shiri.matrices import AdjacencyMatrix, DistanceMatrix, Similarity
from apps.shiri.rules import make_rules
from apps.shiri.search import MCVSPlayer, UCTPlayer


class RulesParityTests(unittest.TestCase):
    def test_breakthrough_moves_match_site_engine(self):
        rules = make_rules("breakthrough")
        rng = random.Random(7)
        fen = bt.STARTING_FEN
        for _ in range(60):
            site = sorted(bt.legal_moves(fen))
            mine = rules.state_from(fen, ply=0)
            self.assertEqual(site, sorted(mine.move_uci(m) for m in mine.legal_moves()))
            if not site:
                break
            grid, turn = bt._fen_to_grid(fen)
            uci = rng.choice(site)
            fr, fc = bt._sq_to_coords(uci[:2])
            tr, tc = bt._sq_to_coords(uci[2:])
            grid[tr][tc], grid[fr][fc] = grid[fr][fc], bt.EMPTY
            site_outcome = bt.get_game_outcome(grid, turn)
            fen = bt._grid_to_fen(grid, bt._opponent_turn(turn))
            nxt = rules.state_from(fen, ply=0)
            self.assertEqual(nxt.fen(), fen)
            self.assertEqual(site_outcome is not None, nxt.result() is not None)
            if site_outcome:
                self.assertEqual(nxt.result(), WHITE if turn == "w" else BLACK)
                break

    def test_chess_roundtrip(self):
        st = make_rules("chess").state_from(chess.STARTING_FEN)
        move = st.parse_uci("e2e4")
        self.assertIsNotNone(move)
        self.assertEqual(st.move_uci(move), "e2e4")
        self.assertIsNone(st.parse_uci("e2e5"))

    def test_chess_promotion_with_and_without_piece_letter(self):
        st = make_rules("chess").state_from("8/4P2k/8/8/8/8/8/K7 w - - 0 1")
        self.assertEqual(st.move_uci(st.parse_uci("e7e8")), "e7e8q")  # bare move auto-queens like the site
        self.assertEqual(st.move_uci(st.parse_uci("e7e8q")), "e7e8q")
        self.assertEqual(st.move_uci(st.parse_uci("e7e8n")), "e7e8n")
        self.assertIsNone(st.parse_uci("e7e8k"))

    def test_abort_stops_a_long_search_early(self):
        import threading
        bot = ShiRi("chess", seed=1, background=False)
        bot.start_new_game("white", 15)
        timer = threading.Timer(0.3, bot.abort)
        timer.start()
        t = time.perf_counter()
        bot.get_move(chess.STARTING_FEN, thinking_time=10)
        timer.cancel()
        self.assertLess(time.perf_counter() - t, 2)


class MatrixTests(unittest.TestCase):
    def test_distance_and_similarity(self):
        adj = AdjacencyMatrix.grid(8, 8)
        dist = DistanceMatrix.from_adjacency(adj)
        self.assertEqual(dist.data[0, 63], 7)
        self.assertEqual(dist.data[0, 0], 0)
        v = np.array([1.0, 0.0], dtype=np.float32)
        self.assertAlmostEqual(Similarity.cosine(v, v), 1.0)

    def test_embedding_is_perspective_symmetric(self):
        for game in ("chess", "breakthrough"):
            rules = make_rules(game)
            emb = BoardEmbedding(rules, connected=True)
            s = rules.initial_state()
            a = emb.vector(s, WHITE)
            b = emb.vector(s, BLACK)
            self.assertEqual(a.shape, (emb.dim,))
            self.assertAlmostEqual(float(np.linalg.norm(a)), 1.0, places=5)
            self.assertGreater(float(a @ b), 0.999)  # start position is symmetric

    def test_tokenized_vector(self):
        tv = TokenizedVector([0, 1, 2, 0], 2)
        self.assertEqual(tv.one_hot().tolist(), [[0, 0], [1, 0], [0, 1], [0, 0]])


class DatabaseTests(unittest.TestCase):
    def test_commit_partitions_and_near_depth(self):
        db = VectorDatabase(4, min_active_records=2)
        v = np.array([1, 0, 0, 0], dtype=np.float32)
        w = np.array([0, 1, 0, 0], dtype=np.float32)
        tr = GameTrace()
        tr.add(v, 10, WHITE)
        tr.add(w, 11, BLACK)
        db.commit_trace(tr.finish(WHITE))
        self.assertEqual((db.size("win"), db.size("loss"), db.size("draw")), (1, 1, 0))
        self.assertAlmostEqual(db.max_similarity_near_depth("win", v, 10, 2), 1.0)
        self.assertEqual(db.max_similarity_near_depth("win", v, 40, 2), 0.0)
        self.assertTrue(db.is_active())

    def test_prune_keeps_limit(self):
        db = VectorDatabase(8, max_per_zone=5)
        rng = np.random.default_rng(1)
        tr = GameTrace()
        for i in range(30):
            vec = rng.normal(size=8).astype(np.float32)
            tr.add(vec / np.linalg.norm(vec), i, WHITE)
        db.commit_trace(tr.finish(DRAW))
        db.prune()
        self.assertEqual(db.size("draw"), 5)


class SearchTests(unittest.TestCase):
    def _legal(self, game, state_arg, uci):
        st = make_rules(game).state_from(state_arg)
        self.assertIn(uci, [st.move_uci(m) for m in st.legal_moves()])

    def test_uct_and_mcvs_respect_time_and_return_legal_moves(self):
        for game in ("chess", "breakthrough"):
            bot = ShiRi(game, seed=1, background=False)
            bot.start_new_game("white", 3)
            state = bot.rules.initial_state()
            fen = state.fen()
            t = time.perf_counter()
            uci = bot.get_move(fen, thinking_time=0.4)
            self.assertLess(time.perf_counter() - t, 0.4 + 0.05)
            self._legal(game, fen, uci)
            # warm the database so the MCVS branch (not the UCT fallback) runs
            runner = AtomicRunner(bot.rules, bot.embedding, bot.db, seed=3)
            deadline = time.time() + 20
            while not bot.db.is_active() and time.time() < deadline:
                runner.run_one_trace()
            self.assertTrue(bot.db.is_active(), game)
            t = time.perf_counter()
            uci = bot.get_move(fen, thinking_time=0.5)
            self.assertLess(time.perf_counter() - t, 0.5 + 0.05)
            self.assertEqual(bot.last_result.mode, "mcvs")
            self._legal(game, fen, uci)

    def test_finds_mate_in_one(self):
        bot = ShiRi("chess", seed=2, background=False)
        bot.start_new_game("black", 3)
        fen = "6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1"
        self.assertEqual(bot.get_move(fen, thinking_time=0.3), "a1a8")

    def test_breakthrough_takes_winning_step(self):
        bot = ShiRi("breakthrough", seed=2, background=False)
        bot.start_new_game("black", 3)
        fen = "8/W7/8/8/8/8/8/7B w"
        self.assertEqual(bot.get_move(fen, thinking_time=0.3), "a7a8")

    def test_accepts_chess_board_object(self):
        bot = ShiRi("chess", seed=4, background=False)
        bot.start_new_game("white", 3)
        board = chess.Board()
        board.push_san("e4")
        uci = bot.get_move(board, thinking_time=0.3)
        self.assertIn(chess.Move.from_uci(uci), board.legal_moves)


class LifecycleTests(unittest.TestCase):
    def test_database_is_reset_between_games(self):
        bot = ShiRi("breakthrough", seed=5, background=False)
        bot.start_new_game("white", 5)
        first_db = bot.db
        runner = AtomicRunner(bot.rules, bot.embedding, bot.db, seed=1)
        end = time.time() + 10
        while bot.db.size() == 0 and time.time() < end:
            runner.run_one_trace()
        self.assertGreater(first_db.size(), 0)
        bot.start_new_game("black", 8)
        self.assertIsNot(bot.db, first_db)
        self.assertEqual(bot.db.size(), 0)
        self.assertEqual(bot.thinking_time, 8)
        self.assertEqual(bot.human_color, BLACK)

    def test_background_runner_fills_database_and_stops(self):
        bot = ShiRi("breakthrough", seed=6)
        try:
            bot.start_new_game("white", 3)
            end = time.time() + 15
            while bot.db.size() == 0 and time.time() < end:
                time.sleep(0.1)
            self.assertGreater(bot.db.size(), 0)
        finally:
            bot.close()
        self.assertIsNone(bot.runner)

    def test_validation(self):
        with self.assertRaises(ValueError):
            ShiRi("go")
        bot = ShiRi("chess", background=False)
        with self.assertRaises(RuntimeError):
            bot.get_move(chess.STARTING_FEN)
        with self.assertRaises(ValueError):
            bot.start_new_game("purple", 5)
        self.assertIn(bot.start_new_game("random", 5), ("white", "black"))
        self.assertEqual(bot.thinking_time, 5)
        bot.start_new_game("white", 99)
        self.assertEqual(bot.thinking_time, 15)


if __name__ == "__main__":
    unittest.main()
