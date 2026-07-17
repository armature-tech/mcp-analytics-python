#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


parser = argparse.ArgumentParser()
parser.add_argument("--artifact", type=Path)
parser.add_argument("--fastmcp-major", choices=("2", "3"), default="3")
parser.add_argument("--require-platform", action="store_true")
options = parser.parse_args()

if options.require_platform:
    required = (
        "SDK_CANARY_INGEST_KEY",
        "SDK_CANARY_READ_API_KEY",
        "SDK_CANARY_MCP_SERVER_ID",
        "SDK_CANARY_PLATFORM_URL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"missing live canary configuration: {', '.join(missing)}")

package_root = Path(__file__).resolve().parents[1]
if options.artifact:
    wheel = options.artifact.resolve()
else:
    dist = package_root / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    run(sys.executable, "-m", "build", str(package_root))
    wheels = list(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, found {wheels}")
    wheel = wheels[0].resolve()

with tempfile.TemporaryDirectory(prefix="armature-python-canary-") as raw_tmp:
    consumer = Path(raw_tmp)
    venv = consumer / "venv"
    run(sys.executable, "-m", "venv", str(venv))
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fastmcp = f"fastmcp>={options.fastmcp_major},<{int(options.fastmcp_major) + 1}"
    run(str(python), "-m", "pip", "install", str(wheel), fastmcp)

    resolved = subprocess.check_output(
        [str(python), "-c", "import armature_mcp_analytics as m; print(m.__file__)"],
        cwd=consumer,
        text=True,
    ).strip()
    if not resolved.startswith(str(venv)):
        raise SystemExit(f"wheel import resolved outside blank consumer: {resolved}")

    tests = consumer / "tests"
    shutil.copytree(package_root / "tests", tests)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["ARMATURE_REQUIRE_FASTMCP"] = "1"
    run(str(python), "-m", "unittest", "discover", "-s", str(tests), cwd=consumer, env=env)
    if options.require_platform:
        run(str(python), str(package_root / "scripts" / "platform_canary.py"), cwd=consumer, env=env)
    print(f"verified {wheel.name} with FastMCP {options.fastmcp_major}.x from {resolved}")
