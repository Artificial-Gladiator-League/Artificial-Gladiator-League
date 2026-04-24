from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Iterable

from django.core.management.base import BaseCommand

from apps.users.model_lifecycle import _user_base_dir, _game_dest_dir
from apps.users.models import UserGameModel

log = logging.getLogger(__name__)


def _read_json_file(path: Path) -> dict | None:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


class Command(BaseCommand):
    help = "Organize user model files into model/ and data/ folders per config files."

    def add_arguments(self, parser):
        parser.add_argument("--user-ids", nargs="*", type=int, help="Limit to these user ids")
        parser.add_argument("--apply", action="store_true", help="Actually perform moves (default: dry-run)")

    def handle(self, *args, **options):
        user_ids = options.get("user_ids") or []
        do_apply = options.get("apply", False)

        qs = UserGameModel.objects.all()
        if user_ids:
            qs = qs.filter(user_id__in=user_ids)

        for gm in qs:
            user_id = gm.user_id
            game = gm.game_type
            base = _user_base_dir(user_id) / game
            if not base.exists():
                self.stdout.write(f"Skipping missing base for user {user_id} game {game}: {base}")
                continue

            # Ensure model/ and data/ exist
            model_dir = base / "model"
            data_dir = base / "data"
            model_dir.mkdir(parents=True, exist_ok=True)
            data_dir.mkdir(parents=True, exist_ok=True)

            # Parse config_model.json at base or under base/model
            cfg = _read_json_file(base / "config_model.json") or _read_json_file(model_dir / "config_model.json")
            moved_files = []
            if cfg:
                # Move any top-level files referenced in config_model.json into model/
                files = cfg.get("files") or []
                if isinstance(files, str):
                    files = [files]
                for fname in files:
                    src_candidates = [base / fname, base / "model" / fname, base / "data" / fname]
                    for src in src_candidates:
                        if src.exists() and src.is_file():
                            dst = model_dir / src.name
                            if src.resolve() == dst.resolve():
                                break
                            moved_files.append((src, dst))
                            if do_apply:
                                try:
                                    dst.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.move(str(src), str(dst))
                                except Exception as e:
                                    self.stdout.write(f"Failed to move {src} -> {dst}: {e}")
                            break

            # Parse config_data.json for data-related files
            dcfg = _read_json_file(base / "config_data.json") or _read_json_file(data_dir / "config_data.json")
            moved_data = []
            if dcfg:
                data_files = dcfg.get("files") or dcfg.get("datasets") or []
                if isinstance(data_files, str):
                    data_files = [data_files]
                for fname in data_files:
                    src_candidates = [base / fname, base / "data" / fname, base / "model" / fname]
                    for src in src_candidates:
                        if src.exists() and src.is_file():
                            dst = data_dir / src.name
                            if src.resolve() == dst.resolve():
                                break
                            moved_data.append((src, dst))
                            if do_apply:
                                try:
                                    dst.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.move(str(src), str(dst))
                                except Exception as e:
                                    self.stdout.write(f"Failed to move {src} -> {dst}: {e}")
                            break

            # Report
            if moved_files or moved_data:
                self.stdout.write(f"User {user_id} game {game}:")
                for s, d in moved_files:
                    self.stdout.write(f"  MODEL: {s} -> {d}")
                for s, d in moved_data:
                    self.stdout.write(f"  DATA: {s} -> {d}")
            else:
                self.stdout.write(f"User {user_id} game {game}: nothing to move")

        self.stdout.write("Done.")
