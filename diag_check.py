from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from apps.users.models import UserGameModel
from apps.games.local_inference import resolve_model_path

u = get_user_model().objects.get(username="yuval")
gm = UserGameModel.objects.get(user_id=u.id, game_type="chess")

print("=== settings ===")
print("USER_MODELS_BASE_DIR:", settings.USER_MODELS_BASE_DIR)
print("HF_HUB_CACHE:", settings.HF_HUB_CACHE)

print("=== DB fields ===")
for name in ("approved_full_sha", "current_repo_sha", "submitted_ref",
             "cached_path", "cached_commit", "approved_data_repo_sha",
             "current_data_repo_sha", "hf_model_repo_id", "hf_data_repo_id"):
    print(f"{name}: {getattr(gm, name, '<no such field>')}")

print("=== HF cache for the model repo ===")
root = Path(settings.HF_HUB_CACHE) / "models--test1978--chess-model"
ref_main = root / "refs" / "main"
print("refs/main ->", ref_main.read_text().strip() if ref_main.exists() else "missing")
snaps_dir = root / "snapshots"
if snaps_dir.exists():
    for s in sorted(snaps_dir.iterdir()):
        files = [(f.name, f.is_symlink(), f.exists())
                 for f in s.rglob("*") if f.is_file() or f.is_symlink()]
        print(s.name[:12], files)
else:
    print("no snapshots folder")

print("=== what the platform resolves ===")
m, d = resolve_model_path(
    u.id, "chess",
    repo_id=gm.hf_model_repo_id,
    model_revision=gm.approved_full_sha,
    data_repo_id=gm.hf_data_repo_id,
    data_revision=(gm.approved_data_repo_sha or "main"),
)
print("model_dir:", m)
print("data_dir:", d)

print("=== per-user folder ===")
ud = Path(settings.USER_MODELS_BASE_DIR) / f"user_{u.id}" / "chess"
for sub in ("model", "data"):
    p = ud / sub
    print(sub, "exists" if p.exists() else "MISSING", [x.name for x in p.rglob("*")] if p.exists() else [])
print("=== done ===")