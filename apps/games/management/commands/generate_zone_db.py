"""
Management command: generate_zone_db
─────────────────────────────────────
Generates a seed ``breakthrough_zone_db.npz`` via UCT self-play.

Usage:
    python manage.py generate_zone_db               # 50 games (default)
    python manage.py generate_zone_db --games 200   # custom count
    python manage.py generate_zone_db --output /path/to/db.npz

The file is written next to ``breakthrough_mcvs.py`` (the project root)
so the server picks it up automatically on next startup.
"""
from __future__ import annotations

import os
import random
import sys
import time

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Generate a seed breakthrough_zone_db.npz via UCT self-play."

    def add_arguments(self, parser):
        parser.add_argument(
            "--games",
            type=int,
            default=50,
            help="Number of self-play games to generate (default: 50).",
        )
        parser.add_argument(
            "--output",
            type=str,
            default="",
            help="Output path for the .npz file. "
                 "Default: project root (next to breakthrough_mcvs.py).",
        )
        parser.add_argument(
            "--time-per-move",
            type=float,
            default=0.3,
            help="UCT search time budget per move in seconds (default: 0.3).",
        )

    def handle(self, *args, **options):
        num_games = options["games"]
        time_per_move = options["time_per_move"]

        # ── Resolve output path ──────────────────────
        if options["output"]:
            output_path = os.path.abspath(options["output"])
        else:
            output_path = self._default_output_path()

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Generating zone DB: {num_games} self-play games → {output_path}"
        ))

        # ── Import breakthrough_mcvs ─────────────────
        mcvs = self._load_mcvs()
        if mcvs is None:
            self.stderr.write(self.style.ERROR(
                "Could not import breakthrough_mcvs. "
                "Make sure breakthrough_mcvs.py exists at the project root."
            ))
            return

        Breakthrough = mcvs.Breakthrough
        UCTSearcher = mcvs.UCTSearcher
        HilbertOrderedZoneDatabase = mcvs.HilbertOrderedZoneDatabase

        # ── Create zone DB (empty or load existing) ──
        zone_db = HilbertOrderedZoneDatabase(output_path, max_size=10000)
        searcher = UCTSearcher(cpuct=np.sqrt(2.0))

        existing_w = len(zone_db.winning_matrices)
        existing_l = len(zone_db.losing_matrices)
        existing_d = len(zone_db.draw_matrices)
        if existing_w + existing_l + existing_d > 0:
            self.stdout.write(
                f"  Loaded existing DB: W={existing_w}  L={existing_l}  D={existing_d}"
            )

        # ── Self-play loop ───────────────────────────
        t0 = time.time()
        wins_p1, wins_p2, draws = 0, 0, 0

        for i in range(1, num_games + 1):
            game = Breakthrough()
            trajectory = [game.copy()]

            while not game.is_terminal():
                moves = game.get_legal_moves()
                if not moves:
                    break

                visits = searcher.search_with_time_budget(game, time_per_move)
                if visits:
                    best_move = max(visits, key=visits.get)
                else:
                    best_move = random.choice(moves)

                game.apply_move(best_move)
                trajectory.append(game.copy())

            winner = game.check_winner()
            if winner == Breakthrough.PLAYER1:
                result = 1
                wins_p1 += 1
            elif winner == Breakthrough.PLAYER2:
                result = -1
                wins_p2 += 1
            else:
                result = 0
                draws += 1

            zone_db.add_game_record(trajectory, result, sample_rate=0.3)

            elapsed = time.time() - t0
            avg = elapsed / i
            eta = avg * (num_games - i)
            self.stdout.write(
                f"  Game {i}/{num_games}  winner={'P1' if result == 1 else 'P2' if result == -1 else 'draw'}  "
                f"moves={game.move_count}  "
                f"elapsed={elapsed:.0f}s  ETA={eta:.0f}s"
            )

        # ── Save ─────────────────────────────────────
        zone_db.save()
        total_elapsed = time.time() - t0

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"✅ Zone DB saved to {output_path}"
        ))
        self.stdout.write(
            f"   {num_games} games in {total_elapsed:.1f}s  "
            f"(P1 wins: {wins_p1}, P2 wins: {wins_p2}, draws: {draws})"
        )
        self.stdout.write(
            f"   W={len(zone_db.winning_matrices)}  "
            f"L={len(zone_db.losing_matrices)}  "
            f"D={len(zone_db.draw_matrices)}"
        )
        self.stdout.write(
            f"   File size: {os.path.getsize(output_path) / 1024:.0f} KB"
        )

    # ── Helpers ──────────────────────────────────────

    def _default_output_path(self) -> str:
        """Return the project-root path for the zone DB file.

        This is the first path that ``_get_zone_db()`` in
        ``predict_breakthrough.py`` checks (``mcvs_dir``).
        """
        # BASE_DIR is the inner agladiator/ that contains manage.py
        base_dir = str(getattr(settings, "BASE_DIR", ""))
        if base_dir and os.path.isfile(os.path.join(base_dir, "manage.py")):
            project_root = os.path.dirname(base_dir)
        else:
            # Fallback: go up from this file
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
            )
        return os.path.join(project_root, "breakthrough_zone_db.npz")

    @staticmethod
    def _load_mcvs():
        """Import breakthrough_mcvs via direct file loading."""
        import importlib.util

        this_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.abspath(
            os.path.join(this_dir, "..", "..", "..", "..")
        )
        mcvs_path = os.path.join(project_root, "breakthrough_mcvs.py")
        if not os.path.isfile(mcvs_path):
            return None

        abc_dir = os.path.dirname(mcvs_path)
        if abc_dir not in sys.path:
            sys.path.insert(0, abc_dir)

        spec = importlib.util.spec_from_file_location("breakthrough_mcvs", mcvs_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["breakthrough_mcvs"] = mod
        return mod
