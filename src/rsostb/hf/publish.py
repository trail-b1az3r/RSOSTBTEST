"""Publishing to Hugging Face: the dataset and the Space.

Tokens are read from an environment variable (``HF_TOKEN`` by default; in
GitHub Actions it comes from repository secrets). Nothing is published unless
the local checks pass first.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..datasets.build import LeakError, build_dataset, check_no_private
from ..datasets.loader import load_benchmark

DEFAULT_DATASET_REPO = "ray0rf1re/RSOSTBTEST-pro"
DEFAULT_SPACE_REPO = "ray0rf1re/RSOSTBTEST-pro"
SPACE_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "private", "*.partial")


def _api(token_env: str):
    token = os.environ.get(token_env)
    if not token:
        raise SystemExit(f"{token_env} is not set; export a Hugging Face write token to publish")
    try:
        from huggingface_hub import HfApi  # type: ignore
    except ImportError as exc:
        raise SystemExit("pip install 'rsostbtest-pro[hf]' to publish") from exc
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
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    api.upload_folder(folder_path=str(out_dir), repo_id=repo, repo_type="dataset",
                      commit_message=f"Sync RSOSTBTEST-pro dataset (hash {bench.dataset_hash[:12]})")
    print(f"published {len(files)} files to https://huggingface.co/datasets/{repo}")
    return 0


def stage_space(repo_root: str | Path, dest: str | Path) -> Path:
    """Assemble a self-contained Space: app + the exact validated package and benchmark."""
    root = Path(repo_root)
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root / "hf" / "space", dest, ignore=SPACE_IGNORE)
    shutil.copytree(root / "src" / "rsostb", dest / "src" / "rsostb", ignore=SPACE_IGNORE)
    bdest = dest / "benchmark"
    for part in ("tasks", "schemas", "configs", "versions", "resources"):
        shutil.copytree(root / "benchmark" / part, bdest / part, ignore=SPACE_IGNORE)
    if (bdest / "private").exists():
        raise LeakError("private tasks must never be staged into the Space")
    return dest


def publish_space(repo_root: str | Path = ".", *, repo: str = DEFAULT_SPACE_REPO, dry_run: bool = False,
                  token_env: str = "HF_TOKEN") -> int:
    staging = Path(tempfile.mkdtemp(prefix="rsostb-space-"))
    try:
        path = stage_space(repo_root, staging / "space")
        n = sum(1 for p in path.rglob("*") if p.is_file())
        if dry_run:
            print(f"(dry run) would upload {n} files to spaces/{repo}")
            return 0
        api = _api(token_env)
        api.create_repo(repo, repo_type="space", space_sdk="gradio", exist_ok=True)
        api.upload_folder(folder_path=str(path), repo_id=repo, repo_type="space",
                          commit_message="Sync RSOSTBTEST-pro Space from GitHub",
                          delete_patterns=["src/**", "benchmark/**"])
        print(f"published {n} files to https://huggingface.co/spaces/{repo}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)
