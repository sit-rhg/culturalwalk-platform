from __future__ import annotations

import subprocess
from pathlib import Path


def test_frontend_startup_line_supports_macos_bash_3_2() -> None:
    script = Path(__file__).resolve().parents[3] / "scripts" / "demo_up.sh"
    command = next(
        line.strip()
        for line in script.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith('say "启动前端')
    )
    completed = subprocess.run(
        [
            "bash",
            "-c",
            "WEB_PORT=5173; say() { printf '\\n== %s\\n' \"$1\"; }; " + command,
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "5173" in completed.stdout
