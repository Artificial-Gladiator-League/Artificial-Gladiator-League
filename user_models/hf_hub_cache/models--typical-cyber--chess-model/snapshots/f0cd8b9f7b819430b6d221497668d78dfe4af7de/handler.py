import sys
import json
import random
from pathlib import Path

model_path = Path("/model")
if str(model_path) not in sys.path:
    sys.path.insert(0, str(model_path))

import chess_mcvs

class EndpointHandler:
    def __init__(self, path: str, game_model: dict = None):
        self.path = Path(path)
        self.config = game_model or {}

        # Load both config files
        for cfg_file in ["config_model.json", "config_data.json"]:
            p = self.path / cfg_file
            if p.exists():
                try:
                    self.config.update(json.loads(p.read_text(encoding="utf-8")))
                except:
                    pass

        self.lambda_zone = float(self.config.get("lambda_zone", 0.6))
        self.k_zone = int(self.config.get("k_zone", 8))
        self.time_per_move = float(self.config.get("time_per_move", 3.0))
        self.temperature = float(self.config.get("temperature", 0.25))

        # === Load Zone Database ===
        self.zone_db = None
        zone_path = self.path / "chess_zone_db.npz"
        if not zone_path.exists():
            zone_path = Path("/data") / "chess_zone_db.npz"   # sandbox mount

        if zone_path.exists():
            try:
                self.zone_db = chess_mcvs.HilbertOrderedZoneDatabase(filepath=str(zone_path))
                print(f"✅ Zone DB LOADED | W={len(self.zone_db.winning_matrices)} | "
                      f"L={len(self.zone_db.losing_matrices)} | D={len(self.zone_db.draw_matrices)}", 
                      file=sys.stderr)
            except Exception as e:
                print(f"❌ Zone DB failed: {e}", file=sys.stderr)
        else:
            print("⚠️ chess_zone_db.npz NOT FOUND", file=sys.stderr)

        # === Create MCVSSearcher (same as your local experiment) ===
        self.searcher = chess_mcvs.MCVSSearcher(
            policy_net=None,
            value_net=None,
            zone_db=self.zone_db,
            use_nets=False,
            lambda_zone=self.lambda_zone,
            k_zone=self.k_zone,
            cpuct=float(self.config.get("cpuct", 1.5)),
            dirichlet_alpha=float(self.config.get("dirichlet_alpha", 0.3)),
            dirichlet_noise_fraction=0.25,
            device="cpu"
        )

        print(f"🚀 Handler ready | lambda_zone={self.lambda_zone} | time={self.time_per_move}s | temp={self.temperature}", 
              file=sys.stderr)

    def __call__(self, data: dict):
        inputs = data.get("inputs", data)
        fen = inputs.get("fen")
        player = inputs.get("player", "w")
        time_budget = float(inputs.get("time_budget", self.time_per_move))

        print(f"🔍 Search start | FEN={fen[:40]}... | time_budget={time_budget:.1f}s", file=sys.stderr)

        game = chess_mcvs.Chess()
        game.board.set_fen(fen)
        if str(player).lower().startswith("b"):
            game.board.turn = False

        # Run the same searcher you used in your experiment
        visits, sims = self.searcher.search_with_time_budget(game, time_budget)

        print(f"📊 Search done → {sims} simulations, explored {len(visits)} moves", file=sys.stderr)

        if visits:
            # Use temperature like in your successful local tests
            if self.temperature > 0.01:
                moves = list(visits.items())
                counts = [c for _, c in moves]
                logits = [c ** (1.0 / self.temperature) for c in counts]
                total = sum(logits)
                probs = [l / total for l in logits]
                
                r = random.random()
                cum = 0.0
                for (move, _), p in zip(moves, probs):
                    cum += p
                    if r <= cum:
                        chosen = move
                        break
                else:
                    chosen = moves[-1][0]
            else:
                chosen = max(visits.items(), key=lambda x: x[1])[0]

            print(f"✅ Final move: {chosen.uci()}", file=sys.stderr)
            return {"move": chosen.uci()}
        else:
            print("⚠️ No visits, using fallback", file=sys.stderr)
            legal = list(game.get_legal_moves())
            move = max(legal, key=lambda m: game.board.is_capture(m)) if legal else legal[0]
            return {"move": move.uci()}


def get_move(data: dict, path: str = ".", game_model: dict = None):
    h = EndpointHandler(path=path, game_model=game_model)
    return h(data)