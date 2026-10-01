"""Publishing to Hugging Face: the dataset and the Space.

Tokens are read from an environment variable (``HF_TOKEN`` by default; in
GitHub Actions it comes from repository secrets). Nothing is published unless
the local checks pass first.

The Space comes in two kinds (``sdk``): ``static``, the leaderboard
pre-computed into one page, which any Hugging Face account can host; and
``gradio``, the interactive app with in-Space uploads, which Hugging Face only
lets paid plans create. ``auto`` (the default for publishing) keeps the kind
of an existing Space and makes a new Space static.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..datasets.build import LeakError, build_dataset, check_no_private
from ..datasets.loader import load_benchmark
from .errors import hub_error, hub_hint
from .static_space import stage_static_space

DEFAULT_DATASET_REPO = "ray0rf1re/RSOSTBTEST-pro"
DEFAULT_SPACE_REPO = "ray0rf1re/RSOSTBTEST-pro"
SPACE_SDKS = ("static", "gradio")
SPACE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "private", "*.partial")
# Every path either kind of Space is built from. Remote files matching these
# and absent from the new bundle are deleted, so switching kinds leaves no
# stale app behind; anything else in the Space repo is left alone.
SPACE_MANAGED = ["app.py", "requirements.txt", "index.html", "leaderboard.json", "src/**", "benchmark/**", "seed/**"]


def _space_api(token_env: str):
    """A Hub client for read-only look-ups (dry runs): the token if set, else anonymous."""
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError:
        return None
    return HfApi(token=os.environ.get(token_env) or None)


def existing_space_sdk(api, repo: str) -> str | None:
    """The SDK of ``spaces/<repo>``, or None if there is no such Space."""
    if not api.repo_exists(repo, repo_type="space"):
        return None
    return getattr(api.space_info(repo), "sdk", None) or "unknown"


def _api(token_env: str):
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(f"{token_env} is not set; export a Hugging Face write token to publish")
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError as exc:
        raise SystemExit("pip install 'RSOSTB[hf]' to publish") from exc
    return HfApi(token=token)


def publish_dataset(out_dir: str | Path = "dataset", *, repo: str = DEFAULT_DATASET_REPO, dry_run: bool = False,
                    token_env: str = "HF_TOKEN") -> int:
    bench = load_benchmark()
    stale = build_dataset(bench, out_dir, check=True)
    if stale:
        print(f"dataset/ is stale ({len(stale)} files); run `rsostb dataset build` and commit first")
        return 1
    files = {p.relative_to(out_dir).as_posix(): p.read_text(encoding="utf-8")
             for p in Path(out_dir).rglob("*") if p.is_file()}
    try:
        check_no_private(files)
    except LeakError as exc:
        print(f"refusing to publish: {exc}")
        return 1
    if dry_run:
        print(f"(dry run) would upload {len(files)} files from {out_dir} to datasets/{repo}")
        return 0
    api = _api(token_env)
    try:
        api.create_repo(repo, repo_type="dataset", exist_ok=True)
        api.upload_folder(folder_path=str(out_dir), repo_id=repo, repo_type="dataset",
                          commit_message=f"Sync RSOSTBTEST-pro dataset (hash {bench.dataset_hash[:12]})")
    except Exception as exc:
        print(f"upload to datasets/{repo} failed: {hub_error(exc)}")
        if hint := hub_hint(exc):
            print(hint)
        return 1
    print(f"published {len(files)} files to https://huggingface.co/datasets/{repo}")
    return 0


def stage_space(repo_root: str | Path, dest: str | Path, *, sdk: str = "static", results_repo: str | None = None,
                token: str | None = None) -> Path:
    """Assemble the Space. ``static``: the pre-computed leaderboard page (entries
    from ``results_repo``, if given, plus the bundled baselines). ``gradio``:
    the app with the exact validated package and benchmark."""
    if sdk not in SPACE_SDKS:
        raise ValueError(f"unknown Space SDK {sdk!r}; use one of {', '.join(SPACE_SDKS)}")
    if sdk == "static":
        return stage_static_space(repo_root, dest, results_repo=results_repo, token=token)
    root = Path(repo_root)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root / "hf" / "space", dest, ignore=SPACE_IGNORE)
    shutil.copytree(root / "src" / "rsostb", dest / "src" / "rsostb", ignore=SPACE_IGNORE)
    seeds = root / "examples" / "results"
    if seeds.is_dir():
        # Baseline runs shown on a fresh leaderboard (re-validated at startup).
        (dest / "seed").mkdir(parents=True, exist_ok=True)
        for p in sorted(seeds.glob("*.json*")):
            shutil.copy2(p, dest / "seed" / p.name)
    bdest = dest / "benchmark"
    for part in ("tasks", "schemas", "configs", "versions", "resources"):
        shutil.copytree(root / "benchmark" / part, bdest / part, ignore=SPACE_IGNORE)
    if (bdest / "private").exists():
        raise LeakError("private tasks must never be staged into the Space")
    return dest


def publish_space(repo_root: str | Path = ".", *, repo: str = DEFAULT_SPACE_REPO, sdk: str = "auto",
                  results_repo: str | None = None, dry_run: bool = False, token_env: str = "HF_TOKEN") -> int:
    if sdk not in ("auto", *SPACE_SDKS):
        raise ValueError(f"unknown Space SDK {sdk!r}; use auto, {', '.join(SPACE_SDKS)}")
    api = _space_api(token_env) if dry_run else _api(token_env)
    existing = None
    if api is not None:
        try:
            existing = existing_space_sdk(api, repo)
        except Exception as exc:
            print(f"could not look up spaces/{repo}: {hub_error(exc)}")
            if hint := hub_hint(exc):
                print(hint)
            if not dry_run:
                return 1
    if sdk == "auto":
        if existing not in (None, *SPACE_SDKS):
            print(f"spaces/{repo} is a {existing} Space; choose --sdk static or --sdk gradio to replace it")
            return 1
        sdk = existing or "static"
        print(f"Space SDK: {sdk} ({'kept from the existing Space' if existing else 'a new Space is static'})")
    staging = Path(tempfile.mkdtemp(prefix="rsostb-space-"))
    try:
        path = stage_space(repo_root, staging / "space", sdk=sdk, results_repo=results_repo,
                           token=os.environ.get(token_env) or None)
        n = sum(1 for p in path.rglob("*") if p.is_file())
        if dry_run:
            print(f"(dry run) would upload {n} files to spaces/{repo} ({sdk} Space)")
            return 0
        try:
            # Creating is only attempted for a new Space: it is the step that
            # needs a paid plan for Gradio, and an existing Space never needs it.
            if existing is None:
                api.create_repo(repo, repo_type="space", space_sdk=sdk)
            api.upload_folder(folder_path=str(path), repo_id=repo, repo_type="space",
                              commit_message=f"Sync RSOSTBTEST-pro Space ({sdk}) from GitHub",
                              delete_patterns=SPACE_MANAGED)
        except Exception as exc:
            print(f"upload to spaces/{repo} failed: {hub_error(exc)}")
            if hint := hub_hint(exc, space_sdk=sdk):
                print(hint)
            return 1
        print(f"published {n} files to https://huggingface.co/spaces/{repo} ({sdk} Space)")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)
