"""
Utilities for the MiMICS notebook demo: locate the repo, build the Java classpath,
check compiled artifacts, and start/stop the Py4J gateway process.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HAL_UPSTREAM = "https://github.com/MathOnco/HAL.git"
PY4J_MAVEN_URL = (
    "https://repo1.maven.org/maven2/net/sf/py4j/py4j/0.10.9.7/py4j-0.10.9.7.jar"
)
PYTHON_PACKAGES = ("cobra", "pandas", "numpy", "openpyxl", "py4j", "matplotlib")


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


def _hal_lwjgl_classpath(repo: Path) -> str:
    lib = repo / "_deps_HAL" / "HAL" / "lib"
    jars = [
        lib / "lwjgl.jar",
        lib / "lwjgl-opengl.jar",
        lib / "lwjgl-glfw.jar",
        lib / "lwjgl-stb.jar",
        lib / "lwjgl-assimp.jar",
        lib / "lwjgl-openal.jar",
    ]
    missing = [j for j in jars if not j.is_file()]
    if missing:
        raise FileNotFoundError(
            "HAL LWJGL jars missing under _deps_HAL/HAL/lib. Clone HAL or restore lib/: "
            + ", ".join(str(m) for m in missing)
        )
    sep = ";" if sys.platform == "win32" else ":"
    return sep.join(str(j) for j in jars)


def _list_hal_java_sources(repo: Path) -> list[Path]:
    root = repo / "_deps_HAL"
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.java"))


def ensure_python_packages(
    packages: tuple[str, ...] = PYTHON_PACKAGES,
    *,
    quiet: bool = True,
) -> list[str]:
    """Install Python dependencies with pip (same interpreter as the notebook)."""
    log: list[str] = []
    args = [sys.executable, "-m", "pip", "install"]
    if quiet:
        args.append("-q")
    args.extend(packages)
    subprocess.check_call(args)
    log.append("pip install: " + " ".join(packages))
    return log


def ensure_py4j_jar(repo: Path) -> Path:
    """Download Py4J jar into ``repo/_jars/`` if not already present."""
    existing = resolve_py4j_jar(repo)
    if existing is not None:
        return existing
    dest_dir = repo / "_jars"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "py4j-0.10.9.7.jar"
    urllib.request.urlretrieve(PY4J_MAVEN_URL, dest)
    return dest


def ensure_hal_repo(repo: Path) -> list[str]:
    """Clone MathOnco/HAL into ``_deps_HAL`` if missing."""
    log: list[str] = []
    hal = repo / "_deps_HAL" / "HAL"
    if hal.is_dir():
        return log
    log.append(f"Cloning HAL into {repo / '_deps_HAL'} ...")
    subprocess.check_call(
        ["git", "clone", "--depth", "1", HAL_UPSTREAM, str(repo / "_deps_HAL")],
        cwd=str(repo),
    )
    return log


def patch_hal_getagents_public(repo: Path) -> tuple[bool, list[str]]:
    """
    Ensure ``GetAgentsRadApprox`` is ``public`` on ``AgentGrid3D`` (MiMICS needs this).
    Returns (changed, log lines).
    """
    log: list[str] = []
    path = repo / "_deps_HAL" / "HAL" / "GridsAndAgents" / "AgentGrid3D.java"
    if not path.is_file():
        return False, log
    text = path.read_text(encoding="utf-8")
    old = text
    # Two overloads — only add public if missing (idempotent)
    text = text.replace(
        "    void GetAgentsRadApprox(final ArrayList<T> retAgentList, final double x, final double y, final double z, final double rad) {",
        "    public void GetAgentsRadApprox(final ArrayList<T> retAgentList, final double x, final double y, final double z, final double rad) {",
    )
    text = text.replace(
        "    void GetAgentsRadApprox(final ArrayList<T> retAgentList, final double x, final double y, final double z, final double rad, AgentToBool<T> EvalAgent) {",
        "    public void GetAgentsRadApprox(final ArrayList<T> retAgentList, final double x, final double y, final double z, final double rad, AgentToBool<T> EvalAgent) {",
    )
    changed = text != old
    if changed:
        path.write_text(text, encoding="utf-8")
        log.append(f"Patched {path} (GetAgentsRadApprox -> public)")
    return changed, log


def hal_getagents_is_public(repo: Path) -> bool:
    """True if compiled ``AgentGrid3D`` exposes public ``GetAgentsRadApprox``."""
    cls_file = repo / "_hal_classes" / "HAL" / "GridsAndAgents" / "AgentGrid3D.class"
    if not cls_file.is_file():
        return False
    try:
        out = subprocess.run(
            [
                "javap",
                "-classpath",
                str(repo / "_hal_classes"),
                "-public",
                "HAL.GridsAndAgents.AgentGrid3D",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return "public void GetAgentsRadApprox(" in out.stdout


def compile_hal(repo: Path) -> list[str]:
    """Compile all HAL sources into ``_hal_classes``."""
    log: list[str] = []
    javac = shutil.which("javac")
    if not javac:
        raise RuntimeError("javac not found on PATH. Install a JDK and retry.")
    sources = _list_hal_java_sources(repo)
    if not sources:
        raise FileNotFoundError("No HAL .java files under _deps_HAL (clone HAL first).")
    libcp = _hal_lwjgl_classpath(repo)
    out = repo / "_hal_classes"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        for s in sources:
            tmp.write(str(s) + "\n")
        argfile = tmp.name
    try:
        subprocess.check_call(
            [
                javac,
                "-encoding",
                "UTF-8",
                "-cp",
                libcp,
                "-d",
                str(out),
                "@" + argfile,
            ]
        )
    finally:
        Path(argfile).unlink(missing_ok=True)
    log.append(f"Compiled HAL -> {out} ({len(sources)} sources)")
    return log


def sync_mimics_java_sources(repo: Path) -> list[str]:
    """Copy MiMICS ``.java`` into ``_java_mimics/MIMICS/``."""
    log: list[str] = []
    ex = repo / "Example_P. aeruginosa biofilm"
    dst_dir = repo / "_java_mimics" / "MIMICS"
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in ("MIMICS_PA.java", "MIMICS_gateway_PA.java"):
        src = ex / name
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, dst_dir / name)
    log.append(f"Synced MiMICS sources -> {dst_dir}")
    return log


def compile_mimics(repo: Path) -> list[str]:
    """Compile MiMICS gateway + model into ``_mimics_classes``."""
    log: list[str] = []
    javac = shutil.which("javac")
    if not javac:
        raise RuntimeError("javac not found on PATH. Install a JDK and retry.")
    py4j = ensure_py4j_jar(repo)
    sync_mimics_java_sources(repo)
    hal_classes = repo / "_hal_classes"
    if not (hal_classes / "HAL").is_dir():
        raise FileNotFoundError("HAL classes missing; run compile_hal first.")
    out = repo / "_mimics_classes"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    d = repo / "_java_mimics" / "MIMICS"
    cp = f"{hal_classes}{':' if sys.platform != 'win32' else ';'}{py4j}"
    subprocess.check_call(
        [
            javac,
            "-encoding",
            "UTF-8",
            "-cp",
            cp,
            "-d",
            str(out),
            str(d / "MIMICS_PA.java"),
            str(d / "MIMICS_gateway_PA.java"),
        ]
    )
    log.append(f"Compiled MiMICS -> {out}")
    return log


def ensure_mimics_demo_ready(
    repo: Path,
    *,
    install_python: bool = True,
    fetch_hal: bool = True,
    compile_java: bool = True,
) -> tuple[bool, list[str]]:
    """
    Best-effort setup for the demo notebook: pip packages, Py4J jar, HAL clone,
    ``GetAgentsRadApprox`` visibility patch, HAL + MiMICS compilation.

    Requires **network** when Py4J jar or HAL repo is missing. Requires **JDK**
    (``javac``/``java``) for compilation.
    """
    lines: list[str] = []
    if install_python:
        lines.extend(ensure_python_packages())
    ensure_py4j_jar(repo)
    lines.append(f"Py4J jar: {resolve_py4j_jar(repo)}")

    if fetch_hal:
        lines.extend(ensure_hal_repo(repo))

    changed, plog = patch_hal_getagents_public(repo)
    lines.extend(plog)

    if not compile_java:
        ok, rest = check_build(repo)
        return ok, lines + rest

    if not _list_hal_java_sources(repo):
        raise FileNotFoundError(
            "HAL sources missing under _deps_HAL. Set fetch_hal=True or clone HAL manually."
        )

    # Compile HAL if missing or GetAgentsRadApprox not public in bytecode
    need_hal = not (
        repo / "_hal_classes" / "HAL" / "GridsAndAgents" / "AgentGrid3D.class"
    ).is_file()
    hal_rebuilt = False
    if need_hal or changed or not hal_getagents_is_public(repo):
        if not need_hal:
            lines.append("Rebuilding HAL (e.g. stale bytecode or non-public GetAgentsRadApprox).")
        lines.extend(compile_hal(repo))
        hal_rebuilt = True
        if not hal_getagents_is_public(repo):
            lines.append(
                "WARNING: javap still does not show public GetAgentsRadApprox; "
                "check AgentGrid3D.java manually."
            )

    # MiMICS if missing, or HAL was rebuilt (relink against updated HAL API)
    cell = repo / "_mimics_classes" / "MIMICS" / "Cell3D_PA.class"
    gw = repo / "_mimics_classes" / "MIMICS" / "MIMICS_gateway_PA.class"
    if hal_rebuilt or not cell.is_file() or not gw.is_file():
        lines.extend(compile_mimics(repo))

    ok, rest = check_build(repo)
    return ok, lines + rest


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


def kill_listeners_on_tcp_port(port: int = 25333) -> list[str]:
    """
    Free Py4J's default port if a previous ``java`` gateway is still listening.
    Uses ``lsof`` on macOS/Linux; on Windows prints a manual hint only.
    """
    msgs: list[str] = []
    if sys.platform == "win32":
        msgs.append(
            f"[port {port}] On Windows, close the old java.exe or free port {port} manually if needed."
        )
        return msgs
    try:
        r = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        msgs.append(
            f"[port {port}] lsof not found; kill any old gateway on port {port} manually."
        )
        return msgs
    pids = [p for p in r.stdout.split() if p.isdigit()]
    for pid in pids:
        subprocess.run(["kill", pid], check=False)
        msgs.append(f"[port {port}] sent SIGTERM to PID {pid}")
    if pids:
        time.sleep(1.0)
        r2 = subprocess.run(
            ["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        for pid in r2.stdout.split():
            if pid.isdigit():
                subprocess.run(["kill", "-9", pid], check=False)
                msgs.append(f"[port {port}] kill -9 PID {pid}")
    return msgs


def start_gateway(
    repo: Path,
    *,
    sleep_sec: float = 10.0,
    port: int = 25333,
    kill_existing: bool = True,
    verbose: bool = True,
) -> subprocess.Popen:
    """Launch ``java -cp ... MIMICS.MIMICS_gateway_PA`` and wait for the JVM to listen."""
    if kill_existing:
        for line in kill_listeners_on_tcp_port(port):
            if verbose:
                print(line)
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
