# Chess model template

This folder is a working example of everything the platform expects from a
chess model repo. Copy it, replace the model logic, and upload.

## Files

- `config_model.json` — the manifest. Every field is optional; missing
  fields fall back to sensible defaults, so old-style repos keep working.
  Fields:
  - `schema_version` (int) — manifest format version. Use `1`.
  - `game_type` (str) — `"chess"` or `"breakthrough"`.
  - `entrypoint` (str) — the `.py` file the platform imports. Default is
    `"<game_type>_mcvs.py"` (e.g. `chess_mcvs.py`).
  - `data_files` (list of str) — filenames your model expects to find in
    your data repo. The platform checks these exist before running your
    model, and shows you a clear error if one is missing.
  - `requirements` (list of str) — informational list of pip packages your
    model needs. The sandbox image is fixed (numpy, python-chess, torch
    CPU); this list is only used to double-check your assumptions during
    the model check, not to install anything.
- `chess_mcvs.py` — the entrypoint. Defines two functions:
  ```python
  state = load(ctx)                    # called once
  move  = get_move(state, fen, player)  # called once per move
  ```
  `ctx.model_dir` / `ctx.data_dir` are real, read-only folders. If you
  declare `load`, the platform does NOT open any data file for you — read
  whatever you need from `ctx.data_dir` inside `load()`.
- `check_my_model.py` — a standalone script (no Django, no Docker) you run
  on your own machine before uploading, to catch problems early.

## Before you upload — checklist

- [ ] Your model file is saved as **UTF-8** (not UTF-16, not a BOM-less
      legacy Windows encoding if you can avoid it — the platform tolerates
      common Windows encodings, but UTF-8 is the safest choice).
- [ ] `config_model.json` is valid JSON (no trailing commas, no comments).
- [ ] Every filename in `data_files` actually exists in your **data** repo.
- [ ] `load(ctx)` runs without raising and returns quickly (it only runs
      once, but a hung `load()` will time out the whole check).
- [ ] `get_move(state, fen, player)` always returns a UCI string
      (e.g. `"e2e4"`, `"e7e8q"` for promotion) — never `None`, never a
      SAN string like `"Nf3"`.
- [ ] You've run `python check_my_model.py /path/to/your/model_dir` locally
      and it reports no problems.
- [ ] Your repo does not open network connections — the sandbox has no
      network access, so any `requests`/`urllib`/etc. call will simply hang
      until the timeout and fail the check.

## Running the local checker

```bash
pip install python-chess
python check_my_model.py /path/to/your/model_dir
```

It performs the same syntax/manifest/sample-position checks the platform's
`apps.games.model_check.check_model()` runs server-side (minus the Docker
sandbox itself), so most problems are caught before you ever upload.
