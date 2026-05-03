from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import json


@dataclass(slots=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    command: list[str]

    def json_or_none(self):
        try:
            return json.loads(self.stdout)
        except Exception:
            return None


def run_python_module(module: str, args: list[str], cwd: str | None = None, timeout: int = 60) -> RunResult:
    cmd = [sys.executable, "-m", module, *args]
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    return RunResult(p.returncode, p.stdout, p.stderr, cmd)


def run_command(cmd: list[str], cwd: str | None = None, timeout: int = 60) -> RunResult:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    return RunResult(p.returncode, p.stdout, p.stderr, cmd)
