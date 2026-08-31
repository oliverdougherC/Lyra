# -*- mode: python ; coding: utf-8 -*-

import runpy
from pathlib import Path

from PyInstaller.building.build_main import Analysis, COLLECT, EXE, PYZ
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

inventory = runpy.run_path(str(Path(SPECPATH) / "component_inventory.py"))
project_root = Path(SPECPATH).parent
DATA_GLOBS = inventory["DATA_GLOBS"]
DYNAMIC_LIB_PACKAGES = inventory["DYNAMIC_LIB_PACKAGES"]
ENTRY_MODULE = inventory["ENTRY_MODULE"]
HIDDENIMPORT_PACKAGES = inventory["HIDDENIMPORT_PACKAGES"]

datas = []
for pattern in DATA_GLOBS:
    datas.extend(collect_data_files("backend", includes=[pattern.removeprefix("backend/")]))

binaries = []
for package in DYNAMIC_LIB_PACKAGES:
    binaries.extend(collect_dynamic_libs(package))

hiddenimports = []
for package in HIDDENIMPORT_PACKAGES:
    if package in {"sympy", "pint"}:
        # Their normal imports and PyInstaller hooks include runtime code. Recursively
        # collecting the packages also bundled their complete test/benchmark trees.
        hiddenimports.append(package)
    else:
        hiddenimports.extend(
            collect_submodules(
                package,
                filter=lambda name: ".tests" not in name
                and ".testsuite" not in name
                and ".benchmarks" not in name,
            )
        )

a = Analysis(
    [str(project_root / (ENTRY_MODULE.replace(".", "/") + ".py"))],
    pathex=[str(project_root)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lyra-backend",
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    strip=False,
    upx=False,
    name="lyra-backend",
)
