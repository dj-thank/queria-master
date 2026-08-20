from __future__ import annotations

import os
import sys
from pathlib import Path


def _normalize_tk_library_paths() -> None:
    """Make frozen Windows Tcl/Tk paths parseable by Tcl 8.6.

    PyInstaller's stock tkinter runtime hook exports backslash paths. Tcl can
    interpret those as malformed Tcl lists on some Windows hosts, so set the
    paths again immediately before importing the desktop application.
    """

    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        root = Path(meipass)
        roots.extend((root, root / "_internal"))
    executable_dir = Path(sys.executable).resolve().parent
    roots.extend((executable_dir, executable_dir / "_internal"))

    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        tcl_dir = root / "_tcl_data"
        tk_dir = root / "_tk_data"
        if not (tcl_dir / "init.tcl").is_file():
            continue
        os.environ["TCL_LIBRARY"] = "{" + tcl_dir.as_posix() + "}"
        if tk_dir.is_dir():
            os.environ["TK_LIBRARY"] = "{" + tk_dir.as_posix() + "}"
        break


_normalize_tk_library_paths()

from queria_master.desktop_app import main


if __name__ == "__main__":
    raise SystemExit(main())
