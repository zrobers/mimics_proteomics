"""
Utilities for the MiMICS notebook demo: locate the repo, build the Java classpath,
check compiled artifacts, and start/stop the Py4J gateway process.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from ``start`` until the example biofilm folder is found."""
    p = (start or Path(__file__).resolve()).parent
    for _ in range(20):
        ex = p / "Example_P. aeruginosa biofilm"
        if ex.is_dir() and (p / "README.md").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise FileNotFoundError(
        "Could not find MiMICS repo root (expected 'Example_P. aeruginosa biofilm' next to README.md)."
    )


def resolve_py4j_jar(repo: Path) -> Path | None:
    """Prefer ``_jars/py4j-*.jar`` in the repo; otherwise search site-packages."""
    jars = sorted((repo / "_jars").glob("py4j*.jar"))
    if jars:
        return jars[0]
    try:
        import site

        for base in site.getsitepackages():
            for path in Path(base).glob("**/py4j*.jar"):
                return path
    except Exception:
        pass
    return None


def java_classpath(repo: Path) -> str:
    """Classpath for ``MIMICS.MIMICS_gateway_PA`` (HAL + MiMICS + Py4J + HAL/lib)."""
    hal_classes = repo / "_hal_classes"
    mimics_classes = repo / "_mimics_classes"
    py4j_jar = resolve_py4j_jar(repo)
    if py4j_jar is None:
        raise FileNotFoundError(
            "Py4J jar not found. Place one under _jars/ or run: pip install py4j"
        )
    hal_lib = repo / "_deps_HAL" / "HAL" / "lib"
    parts: list[str] = [str(mimics_classes), str(hal_classes), str(py4j_jar)]
    if hal_lib.is_dir():
        parts.extend(str(j) for j in sorted(hal_lib.glob("*.jar")))
    sep = ";" if sys.platform == "win32" else ":"
    return sep.join(parts)


def check_build(repo: Path) -> tuple[bool, list[str]]:
    """Return (all_ok, human-readable lines)."""
    lines: list[str] = []
    ok = True
    py4j = resolve_py4j_jar(repo)
    checks = [
        ("HAL compiled classes", repo / "_hal_classes" / "HAL"),
        ("MiMICS compiled classes", repo / "_mimics_classes" / "MIMICS"),
        ("HAL dependency tree (_deps_HAL)", repo / "_deps_HAL" / "HAL"),
        ("Py4J jar", py4j),
    ]
    for label, path in checks:
        if path is not None and path.exists():
            lines.append(f"OK  {label}: {path}")
        else:
            lines.append(f"MISSING {label}: {path}")
            ok = False

    # MiMICS package has multiple top-level classes in one source file.
    # Ensure the package-private agent class exists, or runtime fails with:
    # NoClassDefFoundError: MIMICS/Cell3D_PA
    cell_cls = repo / "_mimics_classes" / "MIMICS" / "Cell3D_PA.class"
    if cell_cls.exists():
        lines.append(f"OK  MiMICS agent class: {cell_cls}")
    else:
        lines.append(
            "MISSING MiMICS agent class: "
            f"{cell_cls} (recompile MIMICS_PA.java and MIMICS_gateway_PA.java)"
        )
        ok = False
    return ok, lines


def start_gateway(
    repo: Path,
    *,
    sleep_sec: float = 10.0,
) -> subprocess.Popen:
    """Launch ``java -cp ... MIMICS.MIMICS_gateway_PA`` and wait for the JVM to listen."""
    cp = java_classpath(repo)
    cmd = ["java", "-cp", cp, "MIMICS.MIMICS_gateway_PA"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(sleep_sec)
    if proc.poll() is not None:
        raise RuntimeError(
            "Java gateway exited immediately. Build HAL/MiMICS and ensure AgentGrid3D "
            "GetAgentsRadApprox is public (see project instructions). Command: "
            + " ".join(cmd)
        )
    return proc


def stop_gateway(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
