# VMTK Upgrade to Python 3.13+

**Repository:** https://github.com/vmtk/vmtk
**Current state:** abandoned, last meaningful activity ~2023
**Goal:** build with Python 3.13+

---

## 1. Core Problem: Dependencies

VMTK's own C++ code does NOT use Python C API directly — all Python bindings are generated via VTK's wrapping system. The main blocker is outdated dependency versions in SuperBuild.

| Dependency | Current | Required for Python 3.13+ | Why |
|---|---|---|---|
| **VTK** | 9.1.0 | **9.5.2** (latest stable: 9.6.0) | VTK 9.1 wrapping code uses removed C API (`tp_print`, etc.) |
| **ITK** | 5.2.1 | **5.4.5** (latest stable) | Python bindings incompatible with 3.13 |
| **vtkAddon** (CMake wrapping) | commit `cf11265...` | **No update needed** | CMake wrapping files unchanged at HEAD `2ed3e22...` |
| **NumPy** | >= 1.20 | **>= 1.26** | 1.20 doesn't support Python 3.13 |

### Recommended approach

Build with `USE_SYSTEM_VTK=ON` and `USE_SYSTEM_ITK=ON` using system/conda packages of VTK 9.5 and ITK 5.4+ already compiled for Python 3.13. This bypasses SuperBuild and its pinned old versions.

---

## 2. Files to Modify

### 2.1. SuperBuild.cmake — dependency versions

**VTK** (line 127):
```cmake
# OLD:
set(VTK_GIT_TAG "v9.1.0")
# NEW (9.5.2 recommended, VMTK already has fixes for 9.5; latest available is 9.6.0):
set(VTK_GIT_TAG "v9.5.2")
```

**ITK** (line 62):
```cmake
# OLD:
set(ITK_GIT_TAG "v5.2.1")
# NEW (5.4.5 latest stable, VMTK already has fixes for 5.4):
set(ITK_GIT_TAG "v5.4.5")
```

### 2.2. CMake/CMakeLists.txt — vtkAddon version

**No update needed.** Verified that all 4 CMake wrapping files are identical between
current commit (`cf11265...`) and latest HEAD main (`2ed3e22...`).
The 4 new commits only touch C++ tests and iostream includes (VTK 9.6 compat).

### 2.3. CMakeLists.txt — CMake version ceiling

```cmake
# OLD:
cmake_minimum_required(VERSION 3.12...3.29.1)
# NEW:
cmake_minimum_required(VERSION 3.16...3.31)
```

### 2.4. CMakeLists.txt — macOS architecture (line 170)

```cmake
# OLD:
set( CMAKE_OSX_ARCHITECTURES "x86_64" CACHE STRING "" FORCE )
# NEW (for Apple Silicon support):
set( CMAKE_OSX_ARCHITECTURES "arm64" CACHE STRING "" FORCE )
```

### 2.5. distribution/conda_build_config.yaml

```yaml
# OLD:
python:
  - 3.10
  - 3.9
  - 3.8
  - 3.7

vtk:
  - 9.1.*

numpy:
  - 1.20

# NEW:
python:
  - 3.13
  - 3.12
  - 3.11

vtk:
  - 9.5.*

itk:
  - 5.4.*

numpy:
  - 1.26
```

---

## 3. Python Code Fixes

### 3.1. vmtkScripts/vmtkimagevolumeviewer.py — remove Python 2 remnants

**Lines 131-137** — `xrange` reference:
```python
# OLD:
PY3 = sys.version_info[0] == 3
if PY3:
    range_func = range
else:
    range_func = xrange

# NEW:
range_func = range
```

**Lines 240-255** — `basestring` reference:
```python
# OLD:
PY3 = sys.version_info[0] == 3
if PY3:
    string_types = str,
else:
    string_types = basestring,

# NEW:
string_types = str,
```

### 3.2. vmtkScripts/vmtkimagesmoothing.py — `basestring` fallback

**Lines 102-107:**
```python
# OLD:
try:
    basestring
except NameError:
    basestring = str

# NEW (just use str directly):
# Remove the try/except, replace isinstance(x, basestring) with isinstance(x, str)
```

### 3.3. Bare `except:` clauses (optional cleanup)

Replace `except:` with `except Exception:` in:
- `PypeS/pypepad.py:299`
- `PypeS/pypescript.py:204, 211, 221`
- `PypeS/pypetestrunner.py:43`
- `PypeS/pyperun.py:22`
- `vtkVmtk/vtkvmtk.py:16, 22`

### 3.4. Remove unnecessary `from __future__` imports (optional)

All `from __future__ import absolute_import` and `unicode_literals` across ~150 vmtkScripts files are no-ops in Python 3. Harmless but dead code.

---

## 4. Known Build Caveats

### 4.1. Stream Tracer

Already marked incompatible with VTK >= 9.2 in `vtkVmtk/CMakeLists.txt`. Ensure `VMTK_BUILD_STREAMTRACER=OFF` when building with VTK 9.4+.

### 4.2. VTK 9.5 API changes

Commit `a95f3e3` ("BUG: Fix compatibility with VTK-9.5") already addresses some VTK 9.5 API changes. Verify these fixes are sufficient.

### 4.3. ITK 5.4 API changes

Commit `6c189dd` ("Updates for compatibility with ITK 5.4 and VTK 9.3") addresses some ITK 5.4 changes. Verify completeness.

### 4.4. `distutils` removal (Python 3.12+)

`distutils` was removed from stdlib in Python 3.12. Found only in commented-out/legacy code:
- `CMakeLists.txt:45` — commented out
- `distribution/legacy/homebrew/vmtk.rb:32` — legacy, not used in main build

No action needed for main build path.

---

## 5. Build Instructions (System VTK/ITK approach)

```bash
# Install dependencies (example for conda)
conda create -n vmtk-dev python=3.13
conda activate vmtk-dev
conda install -c conda-forge vtk=9.5 itk=5.4 numpy cmake ninja

# Configure
mkdir build && cd build
cmake .. \
  -DVMTK_USE_SUPERBUILD=OFF \
  -DUSE_SYSTEM_VTK=ON \
  -DUSE_SYSTEM_ITK=ON \
  -DVTK_VMTK_WRAP_PYTHON=ON \
  -DVMTK_BUILD_STREAMTRACER=OFF \
  -DCMAKE_BUILD_TYPE=Release

# Build
cmake --build . -j$(nproc)
```

---

## 6. Priority Order

1. **[CRITICAL]** Update VTK to 9.4+/9.5 (or use system VTK)
2. **[CRITICAL]** Update ITK to 5.4+ (or use system ITK)
3. ~~**[CRITICAL]** Update vtkAddon CMake wrapping scripts~~ — **not needed**, already up to date
4. **[MEDIUM]** Fix Python 2 remnants (xrange, basestring)
5. **[MEDIUM]** Update conda build config
6. **[LOW]** Clean up bare except clauses
7. **[LOW]** Remove __future__ imports
8. **[LOW]** Fix macOS architecture for Apple Silicon
