"""Clone a CooperBench task's repo at its base commit (once) and print '<repo_dir>|<package_dir>'.

  python coop/prepare_repo.py pallets_click_task 2068

URL and commit come from the task's own setup.sh. The repo is only parsed later, never run.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "external" / "CooperBench" / "dataset"
REPOS = ROOT / "external" / "cb_repos"

# package folder inside each Python repo (what the world graph is built from)
PACKAGE = {
    "pallets_click_task": "src/click",
    "pallets_jinja_task": "src/jinja2",
    "dspy_task": "dspy",
    "dottxt_ai_outlines_task": "outlines",
    "huggingface_datasets_task": "src/datasets",
    "llama_index_task": "llama-index-core/llama_index/core",
    "samuelcolvin_dirty_equals_task": "dirty_equals",
}


def prepare(repo: str, task: str) -> tuple[Path, str]:
    setup = (DATASET / repo / f"task{task}" / "setup.sh").read_text(encoding="utf-8")
    url = re.search(r"git clone (\S+)", setup).group(1)
    commit = re.search(r'BASE_COMMIT="([0-9a-f]{40})"', setup).group(1)
    dest = REPOS / f"{repo}-{task}"
    if not (dest / ".git").exists():
        REPOS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", url, str(dest)], check=True)
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if head != commit:  # parallel runs on the same task must not race on git's index lock
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--force", commit], check=True)
    return dest, PACKAGE[repo]


if __name__ == "__main__":
    d, pkg = prepare(sys.argv[1], sys.argv[2])
    print(f"{d}|{pkg}")
