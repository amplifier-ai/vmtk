#!/usr/bin/env python3
"""
Build a self-contained wheel for vmtk from pre-built Install/ artifacts.

Usage:
    python build_wheel.py [--install-dir build/Install] [--output-dir dist/]

Supports macOS, Linux, and Windows. Auto-detects platform.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# --- Platform detection ---
SYSTEM = platform.system()  # 'Darwin', 'Linux', 'Windows'

# Extension patterns per platform
MODULE_EXT = {
    "Darwin": ".so",
    "Linux": ".so",
    "Windows": ".pyd",
}

LIB_EXT = {
    "Darwin": ".dylib",
    "Linux": ".so",
    "Windows": ".dll",
}

# Where native libs go inside the wheel
LIB_SUBDIR = {
    "Darwin": ".dylibs",
    "Linux": ".libs",
    "Windows": ".libs",
}

# Known Windows system DLLs to never bundle
_WIN_SYSTEM_PREFIXES = [
    "api-ms-", "ext-ms-", "kernel32", "kernelbase", "ntdll", "user32",
    "gdi32", "advapi32", "shell32", "ole32", "oleaut32", "msvcrt",
    "ucrtbase", "vcruntime", "msvcp", "combase", "sechost", "rpcrt4",
    "bcrypt", "cfgmgr32", "crypt32", "ws2_32", "winspool", "comdlg32",
    "shlwapi", "setupapi", "imm32", "version", "winmm", "iphlpapi",
    "userenv", "dbghelp", "mswsock", "opengl32", "python3",
]


def run(cmd, check=True, capture=True, **kwargs):
    """Run a shell command and return output."""
    result = subprocess.run(
        cmd, capture_output=capture, text=True, check=False, **kwargs
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(str(c) for c in cmd)}\n{result.stderr}")
    return result


# ---------------------------------------------------------------------------
# Dependency tracing (platform-specific)
# ---------------------------------------------------------------------------

def get_native_deps(binary_path, lib_dir=None):
    """Get native library dependency names for a binary."""
    if SYSTEM == "Darwin":
        return _get_deps_macos(binary_path)
    elif SYSTEM == "Linux":
        return _get_deps_linux(binary_path, lib_dir)
    elif SYSTEM == "Windows":
        return _get_deps_windows(binary_path, lib_dir)
    return []


def _get_deps_macos(binary_path):
    """Use otool -L to find @rpath/ dependencies."""
    result = run(["otool", "-L", str(binary_path)], check=False)
    if result.returncode != 0:
        return []
    deps = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if line.startswith("@rpath/"):
            deps.append(line.split()[0].replace("@rpath/", ""))
    return deps


def _get_deps_linux(binary_path, lib_dir):
    """Use ldd to find shared library deps from our install dir."""
    result = run(["ldd", str(binary_path)], check=False)
    if result.returncode != 0:
        return []
    lib_dir_str = str(lib_dir) if lib_dir else ""
    deps = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if "=>" in line and "not found" not in line:
            parts = line.split("=>")
            name = parts[0].strip()
            path = parts[1].strip().split()[0] if parts[1].strip() else ""
            if path and lib_dir_str and lib_dir_str in path:
                deps.append(os.path.basename(path))
    return deps


def _is_system_dll(name):
    lower = name.lower()
    return any(lower.startswith(p) for p in _WIN_SYSTEM_PREFIXES)


def _get_deps_windows(binary_path, lib_dir):
    """Use dumpbin /dependents to find DLL deps."""
    result = run(["dumpbin", "/dependents", str(binary_path)], check=False)
    if result.returncode != 0:
        return []
    deps = []
    in_section = False
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if "Image has the following dependencies" in stripped:
            in_section = True
            continue
        if in_section:
            if stripped == "" and deps:
                break
            if stripped.lower().endswith(".dll") and not _is_system_dll(stripped):
                # Only include if it exists in our lib_dir
                if lib_dir and (lib_dir / stripped).exists():
                    deps.append(stripped)
    return deps


# ---------------------------------------------------------------------------
# Rpath / LC_RPATH helpers (macOS-specific, used only on Darwin)
# ---------------------------------------------------------------------------

def _get_lc_rpaths(binary_path):
    result = run(["otool", "-l", str(binary_path)], check=False)
    if result.returncode != 0:
        return []
    rpaths = []
    lines = result.stdout.splitlines()
    for i, line in enumerate(lines):
        if "cmd LC_RPATH" in line:
            for j in range(i + 1, min(i + 4, len(lines))):
                if "path " in lines[j]:
                    path = lines[j].strip().split("path ")[1].split(" (")[0]
                    rpaths.append(path)
                    break
    return rpaths


def _fix_rpath_macos(binary_path, old_rpaths, new_rpath):
    needs_add = True
    for old in old_rpaths:
        if old == new_rpath:
            needs_add = False
            continue
        run(["install_name_tool", "-delete_rpath", old, str(binary_path)], check=False)
    if needs_add:
        run(["install_name_tool", "-add_rpath", new_rpath, str(binary_path)], check=False)


def _codesign(binary_path):
    run(["codesign", "--force", "--sign", "-", str(binary_path)], check=False)


# ---------------------------------------------------------------------------
# Transitive dependency collection
# ---------------------------------------------------------------------------

def collect_needed_libs(module_files, lib_dir):
    """Transitively collect all native lib names needed by the given module files.

    Returns dict: lib_name -> resolved_real_file_path.
    """
    needed = {}
    to_process = set()

    for mod in module_files:
        for dep in get_native_deps(mod, lib_dir):
            if dep not in needed:
                to_process.add(dep)

    while to_process:
        name = to_process.pop()
        candidate = lib_dir / name
        if not candidate.exists():
            print(f"  WARNING: referenced lib {name} not found in {lib_dir}")
            continue
        real_path = candidate.resolve()
        needed[name] = real_path

        for dep in get_native_deps(real_path, lib_dir):
            if dep not in needed and dep not in to_process:
                to_process.add(dep)

    return needed


# ---------------------------------------------------------------------------
# Staging functions
# ---------------------------------------------------------------------------

def find_vmtk_site_packages(install_dir):
    """Find the vmtk package directory in Install."""
    # Unix: lib/python*/site-packages/vmtk or lib/python./site-packages/vmtk
    for base in [install_dir / "lib", install_dir / "Lib"]:
        if not base.exists():
            continue
        # Direct site-packages (Windows conda layout)
        direct = base / "site-packages" / "vmtk"
        if direct.exists():
            return direct
        # python*/site-packages/vmtk
        for d in sorted(base.iterdir()):
            if d.name.startswith("python") and (d / "site-packages" / "vmtk").exists():
                return d / "site-packages" / "vmtk"
    raise FileNotFoundError(f"Cannot find vmtk package in {install_dir}")


def find_vtk_site_packages(install_dir):
    """Find the directory containing vtkmodules/ and vtk.py."""
    for base in [install_dir / "lib", install_dir / "Lib"]:
        if not base.exists():
            continue
        direct = base / "site-packages"
        if (direct / "vtkmodules").exists():
            return direct
        for d in sorted(base.iterdir()):
            if d.name.startswith("python"):
                sp = d / "site-packages"
                if (sp / "vtkmodules").exists():
                    return sp
    raise FileNotFoundError(f"Cannot find vtkmodules in {install_dir}")


def find_lib_dir(install_dir):
    """Find the directory containing native libraries."""
    if SYSTEM == "Windows":
        # Windows: DLLs are in bin/
        bin_dir = install_dir / "bin"
        if bin_dir.exists():
            return bin_dir
    lib_dir = install_dir / "lib"
    if lib_dir.exists():
        return lib_dir
    raise FileNotFoundError(f"Cannot find lib directory in {install_dir}")


def stage_vmtk_package(install_dir, staging_dir):
    """Copy vmtk Python package."""
    mod_ext = MODULE_EXT[SYSTEM]
    src = find_vmtk_site_packages(install_dir)
    dst = staging_dir / "vmtk"

    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))

    n_py = sum(1 for _ in dst.rglob("*.py"))
    n_mod = sum(1 for _ in dst.glob(f"*{mod_ext}"))
    print(f"  Staged vmtk package: {n_py} .py, {n_mod} {mod_ext} files")

    create_vmtk_main(dst)
    if SYSTEM == "Windows":
        _patch_init_for_dll_loading(dst)
    return dst


def create_vmtk_main(vmtk_dir):
    """Create vmtk_main.py entry point."""
    content = '''\
#!/usr/bin/env python3
"""VMTK command-line entry point."""
import sys


def main():
    import vtk
    from vmtk import pypes

    vmtkOptions = ['--help', '--ui', '--file']

    if len(sys.argv) > 1 and sys.argv[1] not in vmtkOptions:
        arguments = sys.argv[1:]
        print("Executing", ' '.join(arguments))
        pipe = pypes.Pype()
        pipe.ExitOnError = 0
        pipe.Arguments = arguments
        pipe.ParseArguments()
        pipe.Execute()
        sys.exit(0)
    elif len(sys.argv) > 1 and '--file' in sys.argv:
        fileindex = sys.argv.index('--file') + 1
        if fileindex < len(sys.argv):
            inputfile = open(sys.argv[fileindex], 'r')
            physicalLines = [line for line in inputfile.readlines()
                           if line and line.strip() and line.strip()[0] != '#']
            lines = []
            for line in physicalLines:
                if lines and lines[-1].endswith('\\\\\\n'):
                    lines[-1] = lines[-1][:-2] + line
                else:
                    lines.append(line)
            for line in lines:
                pipe = pypes.Pype()
                pipe.ExitOnError = 0
                pipe.Arguments = line.split()
                pipe.ParseArguments()
                pipe.Execute()
    elif '--help' in sys.argv:
        print('Usage: \\tvmtk [--ui pad|console]\\t\\tStart in interactive mode\\n'
              '\\tvmtk [PYPE]\\t\\t\\tExecute the pype [PYPE]\\n'
              '\\tvmtk --file [FILE]\\t\\tExecute the content of file [FILE]')
        sys.exit(0)
    else:
        ui = 'pad'
        if '--ui' in sys.argv and sys.argv.index('--ui') != len(sys.argv) - 1:
            ui = sys.argv[sys.argv.index('--ui') + 1]
        if ui == 'pad':
            try:
                from vmtk import pypepad
            except ImportError:
                ui = 'console'
            else:
                pypepad.RunPypeTkPad()
        if ui == 'console':
            try:
                import readline
            except ImportError:
                pass
            else:
                readline.parse_and_bind("tab: complete")
            while 1:
                try:
                    inputString = input("vmtk> ")
                except EOFError:
                    sys.stdout.write('\\n')
                    sys.exit(0)
                if not inputString:
                    continue
                print("Executing", inputString)
                pipe = pypes.Pype()
                pipe.ExitOnError = 0
                pipe.Arguments = inputString.split()
                pipe.ParseArguments()
                try:
                    pipe.Execute()
                except Exception:
                    continue


if __name__ == '__main__':
    main()
'''
    (vmtk_dir / "vmtk_main.py").write_text(content)
    print("  Created vmtk_main.py entry point")


def _patch_init_for_dll_loading(pkg_dir, libs_rel_path=".libs"):
    """On Windows, patch __init__.py to register DLL directory."""
    init_path = pkg_dir / "__init__.py"
    existing = init_path.read_text() if init_path.exists() else ""
    patch = f"""\
import os as _os
import sys as _sys
if _sys.platform == 'win32':
    _libs_dir = _os.path.join(_os.path.dirname(__file__), {libs_rel_path!r})
    if _os.path.isdir(_libs_dir):
        _os.add_dll_directory(_libs_dir)
        _os.environ['PATH'] = _libs_dir + _os.pathsep + _os.environ.get('PATH', '')
"""
    # Insert after __future__ imports and docstrings to avoid SyntaxError
    lines = existing.splitlines(keepends=True)
    insert_pos = 0
    in_docstring = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_docstring:
            insert_pos = i + 1
            if '"""' in stripped or "'''" in stripped:
                in_docstring = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            insert_pos = i + 1
            # Check if docstring opens and closes on same line (e.g. """text""")
            quote = stripped[:3]
            if stripped.count(quote) < 2:
                in_docstring = True
            continue
        if stripped.startswith("from __future__") or stripped.startswith("#") or stripped == "":
            insert_pos = i + 1
        else:
            break
    patched = "".join(lines[:insert_pos]) + patch + "".join(lines[insert_pos:])
    init_path.write_text(patched)


def stage_vtk_package(install_dir, staging_dir):
    """Copy VTK Python package (vtkmodules/ and vtk.py)."""
    mod_ext = MODULE_EXT[SYSTEM]
    vtk_sp = find_vtk_site_packages(install_dir)

    shutil.copytree(
        vtk_sp / "vtkmodules",
        staging_dir / "vtkmodules",
        ignore=shutil.ignore_patterns("__pycache__", "test"),
    )
    shutil.copy2(vtk_sp / "vtk.py", staging_dir / "vtk.py")

    vtkmod_dir = staging_dir / "vtkmodules"
    n_mod = sum(1 for _ in vtkmod_dir.glob(f"*{mod_ext}"))
    n_py = sum(1 for _ in vtkmod_dir.rglob("*.py"))
    print(f"  Staged vtkmodules: {n_py} .py, {n_mod} {mod_ext} files")
    return vtkmod_dir


def stage_native_libs(install_dir, staging_dir):
    """Collect and stage all needed native libraries."""
    mod_ext = MODULE_EXT[SYSTEM]
    lib_dir = find_lib_dir(install_dir)
    subdir = LIB_SUBDIR[SYSTEM]
    libs_dir = staging_dir / "vmtk" / subdir
    libs_dir.mkdir(parents=True, exist_ok=True)

    vmtk_modules = list((staging_dir / "vmtk").glob(f"*{mod_ext}"))
    vtk_modules = list((staging_dir / "vtkmodules").glob(f"*{mod_ext}"))
    all_modules = vmtk_modules + vtk_modules

    print(f"  Scanning {len(all_modules)} {mod_ext} files for native lib dependencies...")
    needed = collect_needed_libs(all_modules, lib_dir)
    print(f"  Found {len(needed)} required native libs")

    for name, real_path in sorted(needed.items()):
        shutil.copy2(real_path, libs_dir / name)

    print(f"  Staged {len(needed)} libs into vmtk/{subdir}/")
    return libs_dir


# ---------------------------------------------------------------------------
# Rpath fixing (platform-specific)
# ---------------------------------------------------------------------------

def fix_all_rpaths(staging_dir, install_dir):
    """Fix library search paths in all staged binaries."""
    if SYSTEM == "Darwin":
        _fix_rpaths_macos(staging_dir, install_dir)
    elif SYSTEM == "Linux":
        _fix_rpaths_linux(staging_dir)
    elif SYSTEM == "Windows":
        pass  # DLL loading handled via __init__.py patch


def _fix_rpaths_macos(staging_dir, install_dir):
    vmtk_dir = staging_dir / "vmtk"
    vtkmod_dir = staging_dir / "vtkmodules"
    dylibs_dir = vmtk_dir / ".dylibs"

    print("  Fixing rpaths (macOS)...")

    for so in vmtk_dir.glob("*.so"):
        _fix_rpath_macos(so, _get_lc_rpaths(so), "@loader_path/.dylibs")
        _codesign(so)

    for so in vtkmod_dir.glob("*.so"):
        _fix_rpath_macos(so, _get_lc_rpaths(so), "@loader_path/../vmtk/.dylibs")
        _codesign(so)

    for dylib in dylibs_dir.glob("*.dylib"):
        _fix_rpath_macos(dylib, _get_lc_rpaths(dylib), "@loader_path")
        _codesign(dylib)

    print("  Rpaths fixed and binaries re-signed")


def _fix_rpaths_linux(staging_dir):
    vmtk_dir = staging_dir / "vmtk"
    vtkmod_dir = staging_dir / "vtkmodules"
    libs_dir = vmtk_dir / ".libs"

    print("  Fixing rpaths (Linux)...")

    for so in vmtk_dir.glob("*.so"):
        run(["patchelf", "--set-rpath", "$ORIGIN/.libs", str(so)], check=False)

    for so in vtkmod_dir.glob("*.so"):
        run(["patchelf", "--set-rpath", "$ORIGIN/../vmtk/.libs", str(so)], check=False)

    for lib in libs_dir.iterdir():
        if lib.suffix in (".so",) or ".so." in lib.name:
            run(["patchelf", "--set-rpath", "$ORIGIN", str(lib)], check=False)

    print("  Rpaths fixed")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_no_external_refs(staging_dir, install_dir):
    """Verify that no binaries reference the old install path."""
    if SYSTEM == "Windows":
        print("  Skipping rpath verification on Windows (uses DLL directories)")
        return True

    mod_ext = MODULE_EXT[SYSTEM]
    lib_ext = LIB_EXT[SYSTEM]
    subdir = LIB_SUBDIR[SYSTEM]
    old_path = str(install_dir / "lib")
    issues = []

    patterns = [
        f"vmtk/*{mod_ext}",
        f"vtkmodules/*{mod_ext}",
        f"vmtk/{subdir}/*{lib_ext}",
    ]
    if SYSTEM == "Linux":
        patterns.append(f"vmtk/{subdir}/*.so.*")

    if SYSTEM == "Darwin":
        for pattern in patterns:
            for binary in staging_dir.glob(pattern):
                rpaths = _get_lc_rpaths(binary)
                if old_path in rpaths:
                    issues.append(f"  {binary.name} still has old rpath: {old_path}")
                for dep in _get_deps_macos(binary):
                    dep_file = staging_dir / "vmtk" / subdir / dep
                    if not dep_file.exists():
                        issues.append(f"  {binary.name} needs {dep} but it's missing from {subdir}/")

    if issues:
        print("WARNING: Unresolved references found:")
        for issue in issues:
            print(issue)
        return False
    print("  All references verified OK")
    return True


# ---------------------------------------------------------------------------
# Setup files and wheel building
# ---------------------------------------------------------------------------

def create_setup_files(staging_dir, version):
    """Create pyproject.toml and metadata in staging directory."""
    mod_ext = MODULE_EXT[SYSTEM]
    lib_ext = LIB_EXT[SYSTEM]
    subdir = LIB_SUBDIR[SYSTEM]

    vmtk_data = [f'"*{mod_ext}"', f'"{subdir}/*{lib_ext}"', '"share/*.xml"']
    vtk_data = [f'"*{mod_ext}"', f'"**/*{mod_ext}"']

    if SYSTEM == "Linux":
        vmtk_data.append(f'"{subdir}/*.so.*"')

    vmtk_data_str = ",\n    ".join(vmtk_data)
    vtk_data_str = ",\n    ".join(vtk_data)

    pyproject = f"""\
[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "amplifierai-vmtk"
version = "{version}"
description = "vmtk - the Vascular Modeling Toolkit"
requires-python = ">=3.13"
dependencies = ["numpy>=1.26"]

[project.scripts]
vmtk = "vmtk.vmtk_main:main"

[tool.setuptools.packages.find]
include = ["vmtk*", "vtkmodules*"]

[tool.setuptools.package-data]
vmtk = [
    {vmtk_data_str}
]
vtkmodules = [
    {vtk_data_str}
]

[tool.setuptools]
py-modules = ["vtk"]
"""
    (staging_dir / "pyproject.toml").write_text(pyproject)
    (staging_dir / "README.md").write_text(
        "# vmtk - the Vascular Modeling Toolkit\n\n"
        "See https://www.vmtk.org for documentation.\n"
    )
    init = staging_dir / "vtkmodules" / "__init__.py"
    if not init.exists():
        init.write_text("")


def _get_platform_tag():
    py_ver = f"cp{sys.version_info.major}{sys.version_info.minor}"
    arch = platform.machine()
    if SYSTEM == "Darwin":
        plat = f"macosx_11_0_{arch}"
    elif SYSTEM == "Linux":
        plat = f"linux_{arch}"
    elif SYSTEM == "Windows":
        plat = "win_amd64" if arch in ("AMD64", "x86_64") else f"win_{arch.lower()}"
    else:
        plat = f"{SYSTEM.lower()}_{arch}"
    return py_ver, plat


def build_wheel(staging_dir, output_dir):
    """Build wheel and fix platform tags."""
    print("  Building wheel...")
    run(
        [sys.executable, "-m", "build", "--wheel",
         "--outdir", str(output_dir), str(staging_dir)],
        capture=False,
    )

    wheels = sorted(output_dir.glob("amplifierai_vmtk-*.whl"))
    if not wheels:
        raise RuntimeError("No wheel file produced!")

    wheel_path = wheels[-1]
    py_ver, plat_tag = _get_platform_tag()

    print(f"  Fixing wheel tags to {py_ver}-{py_ver}-{plat_tag}...")
    run([
        sys.executable, "-m", "wheel", "tags",
        f"--python-tag={py_ver}", f"--abi-tag={py_ver}",
        f"--platform-tag={plat_tag}",
        str(wheel_path),
    ])

    new_wheels = sorted(output_dir.glob(f"amplifierai_vmtk-*-{py_ver}-{py_ver}-{plat_tag}.whl"))
    if new_wheels and new_wheels[-1] != wheel_path:
        wheel_path.unlink()
        wheel_path = new_wheels[-1]

    return wheel_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Build a self-contained vmtk wheel from pre-built artifacts"
    )
    parser.add_argument("--install-dir", default="build/Install")
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--version", default="1.6.0")
    args = parser.parse_args()

    install_dir = Path(args.install_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not install_dir.exists():
        print(f"ERROR: Install directory not found: {install_dir}")
        sys.exit(1)

    print(f"Building vmtk wheel v{args.version} ({SYSTEM} {platform.machine()})")
    print(f"  Install dir: {install_dir}")
    print(f"  Output dir:  {output_dir}")

    with tempfile.TemporaryDirectory(prefix="vmtk-wheel-") as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir()

        print("\n[1/6] Staging vmtk package...")
        stage_vmtk_package(install_dir, staging)

        print("\n[2/6] Staging VTK package...")
        stage_vtk_package(install_dir, staging)

        print("\n[3/6] Collecting and staging native libraries...")
        stage_native_libs(install_dir, staging)

        print("\n[4/6] Fixing library paths...")
        fix_all_rpaths(staging, install_dir)

        print("\n[5/6] Verifying references...")
        ok = verify_no_external_refs(staging, install_dir)

        print("\n[6/6] Creating setup files and building wheel...")
        create_setup_files(staging, args.version)
        wheel_path = build_wheel(staging, output_dir)

    size_mb = wheel_path.stat().st_size / (1024 * 1024)
    print(f"\nWheel created: {wheel_path.name}")
    print(f"Size: {size_mb:.1f} MB")
    print(f"Location: {wheel_path}")

    if not ok:
        print("\nWARNING: Some references could not be verified.")
        sys.exit(1)


if __name__ == "__main__":
    main()
