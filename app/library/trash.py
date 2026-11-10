"""Move files to the Windows Recycle Bin (never a permanent delete) and decide which paths may be removed at all.

The move is the Windows shell's own SHFileOperationW with FOF_ALLOWUNDO. That call deletes for good, without asking, when a
file is too big for the Recycle Bin or sits on a drive that has none (a network or removable drive). So `check_recyclable`
looks at both first and refuses; nothing is then deleted for good by accident.

`inside` is the path guard: a path may only be removed when it is a real file that sits under one of the folders the app knows
(LoRA, model, ComfyUI workflow and wildcard folders), with no link (symlink or junction) anywhere below that folder.
"""
import ctypes
import os
import re
import sys
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 3
FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
FLAGS = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
DRIVE_FIXED = 3
MIB = 1024 * 1024
BITBUCKET = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\"


class TrashError(Exception):
    """A file could not be moved to the Recycle Bin; the message is meant for the user."""
    moved: list = []             # the files that went before this one failed (set by trash_files)


class _FileOp(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                ("fFlags", wintypes.WORD), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


if sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 4:
    _FileOp._pack_ = 1          # the 32-bit shell header packs this structure; the 64-bit one uses the normal alignment


# ---------- the path guard ----------

def inside(path: Path, roots: list[Path]) -> Path | None:
    """The path as a file below one of the roots, or None when it must not be touched: it is outside every root, it is
    a root itself, it goes through `..`, or a symlink or junction sits anywhere between the root and the file."""
    absolute = os.path.normcase(os.path.abspath(path))
    for root in roots:
        top = os.path.normcase(os.path.abspath(root))
        if not absolute.startswith(top + os.sep):
            continue
        relative = absolute[len(top) + 1:]
        if os.path.normcase(os.path.realpath(path)) != os.path.join(os.path.normcase(os.path.realpath(root)), relative):
            return None              # a link below the root points somewhere else
        return Path(path) if Path(path).is_file() else None
    return None


# ---------- can this file go to the Recycle Bin? ----------

def _volume_guid(path: Path) -> str | None:
    """The {guid} of the volume holding `path`, as the Recycle Bin settings in the registry name it."""
    mount = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetVolumePathNameW(str(path), mount, 260):
        return None
    name = ctypes.create_unicode_buffer(64)
    if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(mount.value, name, 64):
        return None
    found = re.search(r"\{[^}]+\}", name.value)
    return found.group(0) if found else None


def recycle_limit(path: Path) -> int | None:
    """Biggest file (bytes) the Recycle Bin of this drive takes, 0 when recycling is switched off there, or None when
    Windows has no setting stored (nothing to check)."""
    import winreg
    guid = _volume_guid(path)
    if not guid:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, BITBUCKET + guid) as key:
            nuke, limit = _value(key, "NukeOnDelete"), _value(key, "MaxCapacity")
    except OSError:
        return None
    return 0 if nuke else (limit * MIB if limit else None)


def _value(key, name: str):
    import winreg
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def check_recyclable(path: Path) -> str | None:
    """Why this file cannot go to the Recycle Bin (windows would delete it for good), or None when it can."""
    if sys.platform != "win32":
        return "The Recycle Bin is only available on Windows."
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    if ctypes.windll.kernel32.GetDriveTypeW(root) != DRIVE_FIXED:
        return f"{root} is not a fixed drive, so it has no Recycle Bin."
    limit = recycle_limit(path)
    if limit == 0:
        return f"The Recycle Bin of {root} is switched off (files would be deleted for good)."
    if limit is not None and path.stat().st_size > limit:
        return f"It is larger than the Recycle Bin of {root} ({limit // MIB // 1024} GB), so Windows would delete it for good."
    return None


# ---------- the move ----------

def to_recycle_bin(path: Path):
    """Move one file to the Recycle Bin. Raises TrashError if Windows says no or the file is still there afterwards."""
    path = Path(path)
    why = check_recyclable(path)
    if why:
        raise TrashError(f"{path.name} was not moved: {why}")
    names = ctypes.create_unicode_buffer(str(path) + "\0")            # a list of names ends with two nulls
    op = _FileOp(None, FO_DELETE, ctypes.addressof(names), None, FLAGS, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or path.exists():
        raise TrashError(f"{path.name} could not be moved to the Recycle Bin (Windows error {code:#x}). Is it open in another program?")


def trash_files(paths: list[Path]) -> list[Path]:
    """Move the files one by one, in the order given, and stop at the first that fails. The caller puts the main file first,
    so when that one fails nothing at all has moved. Returns the files moved. A TrashError carries `moved` (the files that
    went before the failure)."""
    moved = []
    for path in paths:
        try:
            to_recycle_bin(path)
        except TrashError as e:
            e.moved = moved
            raise
        moved.append(path)
    return moved
