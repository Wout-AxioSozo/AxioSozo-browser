"""Reconstruct pinned source checkouts without hooks, resets, or global configuration."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=None, capture=False):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *map(str, args)],
                          cwd=cwd or ROOT, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def ensure(name):
    pin = json.loads((ROOT / "upstreams.lock.json").read_text())[name]
    destination = ROOT / pin["source_dir"]
    if destination.exists():
        actual = git("rev-parse", "HEAD", cwd=destination, capture=True).strip()
        if actual != pin["revision"]:
            raise RuntimeError(f"{name}: existing checkout does not match pin; preserved")
        # Existing local modifications are deliberately retained. Strict overlay validates
        # touched base files. No reset/clean is used, even after interrupted setup.
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    git("clone", "--filter=blob:none", "--depth=1", "--no-checkout", pin["repository"], destination)
    git("fetch", "--depth=1", "origin", pin["revision"], cwd=destination)
    if name == "t3code":
        git("sparse-checkout", "init", "--cone", cwd=destination)
        git("sparse-checkout", "set", "apps/server/src/provider", "packages/effect-codex-app-server", "docs/internals", cwd=destination)
    git("checkout", "--detach", pin["revision"], cwd=destination)
    # Keep no branch: a `git clone --no-local` of this checkout (the Zen build
    # stage) would also copy the default branch, whose blobs were never fetched.
    for ref in git("for-each-ref", "--format=%(refname)", "refs/heads/", cwd=destination, capture=True).split():
        git("update-ref", "-d", ref, cwd=destination)
    actual = git("rev-parse", "HEAD", cwd=destination, capture=True).strip()
    if actual != pin["revision"]:
        raise RuntimeError(f"{name}: revision verification failed")


if __name__ == "__main__":
    for upstream in ("zen", "t3code"):
        ensure(upstream)
