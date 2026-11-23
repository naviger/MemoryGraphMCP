"""Install a repository-local MemoryGraphMCP post-commit hook."""
import argparse
import shlex
import subprocess
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spool", type=Path)
    parser.add_argument("--index-branch", help="Local branch authorized to refresh the architecture index")
    args = parser.parse_args()
    hooks = subprocess.check_output(["git", "rev-parse", "--git-path", "hooks"], text=True)
    hook = Path(hooks.strip()).resolve() / "post-commit"
    script = Path(__file__).resolve().with_name("capture_commit.py")
    if not script.is_file():
        parser.error("capture_commit.py is missing")
    command = [sys.executable, str(script), str(args.spool.resolve())]
    if args.index_branch:
        command += ["--index-branch", args.index_branch]
    hook.parent.mkdir(parents=True, exist_ok=True)
    args.spool.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with hook.open("x") as file:
            file.write("#!/bin/sh\n" + shlex.join(command) + "\n")
    except FileExistsError:
        parser.error(f"{hook} already exists. Add this command to that hook:\n{shlex.join(command)}")
    hook.chmod(0o755)
    print(hook)


if __name__ == "__main__":
    main()
