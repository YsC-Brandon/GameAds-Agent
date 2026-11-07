"""Build, download and install into a fresh temporary venv; preserve real logs."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import venv
from datetime import UTC, datetime
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    evidence = Path(sys.argv[1]).resolve()
    evidence.mkdir(exist_ok=False)
    workspace = Path(tempfile.mkdtemp(prefix="gameads-install-"))
    wheelhouse = workspace / "wheelhouse"
    wheelhouse.mkdir()
    results = []

    def run(name, command, *, cwd=workspace, env=None):
        with (evidence / (name + ".stdout.log")).open("xb") as stdout:
            with (evidence / (name + ".stderr.log")).open("xb") as stderr:
                started = datetime.now(UTC).isoformat()
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=600,
                )
        results.append(
            {
                "name": name,
                "command": list(map(str, command)),
                "exit_code": result.returncode,
                "started_utc": started,
                "finished_utc": datetime.now(UTC).isoformat(),
            }
        )
        (evidence / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        if result.returncode:
            raise RuntimeError(name + " failed; see captured logs")

    run(
        "wheel",
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheelhouse),
            str(root),
        ],
    )
    run(
        "download",
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(wheelhouse),
            "-r",
            str(root / "requirements-windows-py311.lock"),
        ],
    )
    venv.EnvBuilder(with_pip=True).create(workspace / "venv")
    python = workspace / "venv/Scripts/python.exe"
    wheel = next(wheelhouse.glob("gameads_agent-*.whl"))
    run(
        "install",
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "-c",
            str(root / "requirements-windows-py311.lock"),
            str(wheel),
        ],
    )
    run("pip-check", [str(python), "-m", "pip", "check"])
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GAMEADS_")}
    env.update(GAMEADS_LLM_API_KEY="", GAMEADS_LLM_MODEL="")
    run("cli", [str(python), "-m", "app", "list"], env=env)
    run("task-help", [str(python), "-m", "app.task_cli", "--help"], env=env)
    run(
        "candidate-init",
        [
            str(python),
            "-m",
            "app.phase4.maintenance",
            "init",
            "--database",
            str(workspace / "synthetic.sqlite3"),
        ],
        env=env,
    )
    run(
        "candidate-check",
        [
            str(python),
            "-m",
            "app.phase4.maintenance",
            "check",
            "--database",
            str(workspace / "synthetic.sqlite3"),
        ],
        env=env,
    )
    artifacts = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in wheelhouse.glob("*.whl")
    }
    (evidence / "wheel-sha256.json").write_text(json.dumps(artifacts, indent=2), encoding="utf-8")
    (evidence / "environment.json").write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "isolated_python": str(python),
                "system_site_packages": False,
                "network": "dependency downloads only; no model or advertising calls",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
