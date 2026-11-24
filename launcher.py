#!/usr/bin/env python3
"""Create an isolated environment once, then start the local application."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main():
    if sys.version_info < (3, 10):
        raise SystemExit("Image Annotator needs Python 3.10 or newer. Install Python from python.org, then run this launcher again.")
    env_dir = ROOT / ".venv"
    executable = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.exists():
        print("First run: creating a private Python environment…", flush=True)
        venv.EnvBuilder(with_pip=True).create(env_dir)
    digest = hashlib.sha256((ROOT/"requirements.txt").read_bytes()).hexdigest()
    stamp = env_dir / "imageannotator-dependencies.txt"
    if not stamp.exists() or stamp.read_text().strip() != digest:
        print("Installing image dependencies. This first setup needs an internet connection…", flush=True)
        result = subprocess.run([str(executable),"-m","pip","install","-r",str(ROOT/"requirements.txt")])
        if result.returncode:
            print("\nSetup did not finish. Check your internet connection, then try again.")
            return result.returncode
        stamp.write_text(digest)
    return subprocess.call([str(executable),str(ROOT/"app.py"),*sys.argv[1:]],cwd=ROOT)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nImage Annotator closed.")
