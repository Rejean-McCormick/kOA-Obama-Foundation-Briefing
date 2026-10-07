from __future__ import annotations

import ctypes
import fnmatch
import json
import os
import shutil
import struct
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any


# -----------------------------------------------------------------------------
# SmartSnap
# - No console (.pyw)
# - Recursively snapshots this script's folder
# - ZIP compression: DEFLATE, level 2
# - Exclusions aligned with Smart Dumper
# - Places SmartSnap.zip on the Windows clipboard as a pasteable file
# - Leaves NO ZIP in the project folder
# - Keeps only a temporary backing ZIP while the clipboard still references it
# - Deletes the temporary ZIP when the clipboard changes
# - SmartSnapKill.pyw can force-stop all active SmartSnap instances and cleanup
# - Shows OK / FAIL for ~0.5 second
# -----------------------------------------------------------------------------

ALWAYS_IGNORE_DIRS: set[str] = {
    ".git", ".svn", ".hg", ".idea", ".vscode", ".ipynb_checkpoints",
    "node_modules", "venv", ".venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", "coverage", "target", "out",
    "abstract_wiki_architect.egg-info", "WEB-INF", "obj",
}

ALWAYS_IGNORE_EXT: set[str] = {
    ".pyc", ".pyo", ".pyd", ".exe", ".dll", ".so", ".dylib", ".class",
    ".jar", ".war", ".bin", ".iso", ".img", ".log", ".sqlite", ".db",
    ".zip", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".svg", ".lock", ".pdf", ".mp4", ".mp3",
}

ALWAYS_IGNORE_FILES: set[str] = {
    ".env", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
    "Gemfile.lock", "poetry.lock", "Cargo.lock", ".DS_Store", "Thumbs.db",
    "Entity", "Fact", "Modifier", "Predicate", "Property",
}

OUTPUT_NAME = "SmartSnap.zip"
INDEX_NAME = "SNAPSHOT_INDEX.txt"
USE_SMARTIGNORE_EXCLUDE = True
STATE_DIR_NAME = "SmartSnapClipboard"


class IgnoreEngine:
    """Standalone equivalent of Smart Dumper's ignore matching."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.rules: list[dict[str, Any]] = []
        self.smartignore_patterns: list[str] = []

    @staticmethod
    def _parse_ignore_line(raw: str) -> dict[str, Any] | None:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            return None
        if line.startswith("\ufeff"):
            line = line.lstrip("\ufeff")
        while line.endswith(" ") and not line.endswith("\\ "):
            line = line[:-1]
        if line.endswith("\\ "):
            line = line[:-2] + " "
        if line.strip() == "":
            return None

        escaped_prefix = line.startswith("\\#") or line.startswith("\\!")
        if escaped_prefix:
            line = line[1:]
        if line.startswith("#") and not escaped_prefix:
            return None

        neg = False
        if line.startswith("!") and not escaped_prefix:
            neg = True
            line = line[1:]
            if line == "":
                return None

        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = line.startswith("/")
        if anchored:
            line = line[1:]
        if line == "":
            return None

        return {"pattern": line, "neg": neg, "dir_only": dir_only, "anchored": anchored}

    def load(self) -> None:
        visited: set[Path] = set()
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=True):
            current = Path(dirpath)
            try:
                current_real = current.resolve()
            except OSError:
                dirnames[:] = []
                continue

            if current_real in visited:
                dirnames[:] = []
                continue
            visited.add(current_real)
            try:
                current_real.relative_to(self.root)
            except ValueError:
                dirnames[:] = []
                continue

            safe_dirs: list[str] = []
            for name in dirnames:
                if name in ALWAYS_IGNORE_DIRS:
                    continue
                p = current / name
                try:
                    resolved = p.resolve()
                    resolved.relative_to(self.root)
                except (OSError, ValueError):
                    continue
                safe_dirs.append(name)
            dirnames[:] = safe_dirs

            for ignore_name in (".gitignore", ".smartignore"):
                if ignore_name not in filenames:
                    continue
                ignore_path = current / ignore_name
                try:
                    with ignore_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                        for raw in handle:
                            parsed = self._parse_ignore_line(raw)
                            if parsed is not None:
                                parsed["base"] = current_real
                                self.rules.append(parsed)
                except OSError:
                    pass

        smart_path = self.root / ".smartignore"
        try:
            for raw in smart_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    self.smartignore_patterns.append(line)
        except OSError:
            pass

    @staticmethod
    def _match_rule(rule: dict[str, Any], path: Path, is_dir: bool) -> bool:
        base: Path = rule["base"]
        try:
            rel_from_base = path.relative_to(base).as_posix()
        except ValueError:
            return False
        name = path.name
        pattern: str = rule["pattern"]

        if rule["dir_only"]:
            if not is_dir:
                return False
            if rule["anchored"] or "/" in pattern:
                if fnmatch.fnmatch(rel_from_base, pattern):
                    return True
                return rel_from_base == pattern or rel_from_base.startswith(pattern + "/")
            return fnmatch.fnmatch(name, pattern)

        if rule["anchored"] or "/" in pattern:
            return fnmatch.fnmatch(rel_from_base, pattern)
        return fnmatch.fnmatch(name, pattern)

    def gitignore_match(self, path: Path, *, is_dir: bool) -> bool:
        path = path.resolve()
        ignored = False
        for rule in self.rules:
            base: Path = rule["base"]
            try:
                path.relative_to(base)
            except ValueError:
                continue
            if self._match_rule(rule, path, is_dir):
                ignored = not rule["neg"]
        return ignored

    def smartignore_match(self, rel_posix: str, *, is_dir: bool) -> bool:
        if not USE_SMARTIGNORE_EXCLUDE or not self.smartignore_patterns:
            return False

        path = rel_posix
        basename = path.rsplit("/", 1)[-1]
        for raw_pattern in self.smartignore_patterns:
            pattern = raw_pattern.strip()
            if not pattern:
                continue
            if pattern.endswith("/"):
                pattern = pattern[:-1].strip()
                if not pattern or not is_dir:
                    continue
            if pattern.startswith("/"):
                if fnmatch.fnmatch(path, pattern[1:]):
                    return True
                continue
            if "/" not in pattern:
                if fnmatch.fnmatch(basename, pattern):
                    return True
                continue
            if fnmatch.fnmatch(path, pattern):
                return True
            parts = path.split("/")
            for i in range(1, len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), pattern):
                    return True
        return False

    def dir_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return False
        if path.name in ALWAYS_IGNORE_DIRS:
            return False
        if self.smartignore_match(rel, is_dir=True):
            return False
        if self.gitignore_match(resolved, is_dir=True):
            return False
        return True

    def file_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            rel_path = resolved.relative_to(self.root)
        except (OSError, ValueError):
            return False
        if not resolved.is_file():
            return False

        for parent in resolved.parents:
            if parent == self.root:
                break
            try:
                rel_parent = parent.relative_to(self.root).as_posix()
            except ValueError:
                return False
            if parent.name in ALWAYS_IGNORE_DIRS:
                return False
            if self.smartignore_match(rel_parent, is_dir=True):
                return False
            if self.gitignore_match(parent, is_dir=True):
                return False

        if resolved.name in ALWAYS_IGNORE_FILES:
            return False
        if resolved.suffix.lower() in ALWAYS_IGNORE_EXT:
            return False

        rel = rel_path.as_posix()
        if self.smartignore_match(rel, is_dir=False):
            return False
        if self.gitignore_match(resolved, is_dir=False):
            return False
        return True


def collect_files(root: Path, engine: IgnoreEngine) -> list[Path]:
    files: list[Path] = []
    visited: set[Path] = set()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        current = Path(dirpath)
        try:
            current_real = current.resolve()
        except OSError:
            dirnames[:] = []
            continue
        if current_real in visited:
            dirnames[:] = []
            continue
        visited.add(current_real)
        try:
            current_real.relative_to(root)
        except ValueError:
            dirnames[:] = []
            continue

        dirnames[:] = [name for name in dirnames if engine.dir_allowed(current / name)]
        for name in filenames:
            candidate = current / name
            if engine.file_allowed(candidate):
                files.append(candidate.resolve())

    files.sort(key=lambda p: p.relative_to(root).as_posix().lower())
    return files


def build_zip(root: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.partial")
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass

    engine = IgnoreEngine(root)
    engine.load()
    files = collect_files(root, engine)

    try:
        with zipfile.ZipFile(
            temp,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=2,
            strict_timestamps=False,
        ) as archive:
            relative_files = [source.relative_to(root).as_posix() for source in files]
            for source, relative_path in zip(files, relative_files):
                archive.write(source, arcname=relative_path)

            index_lines = [
                "SmartSnap snapshot file index",
                f"Files included: {len(relative_files)}",
                "",
                *relative_files,
                "",
                f"Index file: {INDEX_NAME}",
            ]
            archive.writestr(INDEX_NAME, "\n".join(index_lines) + "\n")
        os.replace(temp, output)
        return output
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def state_root() -> Path:
    return Path(tempfile.gettempdir()) / STATE_DIR_NAME


def _filetime_value(ft: Any) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def current_process_creation_time() -> int:
    if os.name != "nt":
        return 0

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int

    created = FILETIME()
    exited = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    handle = kernel32.GetCurrentProcess()
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("GetProcessTimes failed")
    return _filetime_value(created)


def process_matches(pid: int, creation_time: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        created = FILETIME()
        exited = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            return False
        return _filetime_value(created) == creation_time
    finally:
        kernel32.CloseHandle(handle)


def cleanup_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def safe_instance_dir(raw: object) -> Path | None:
    try:
        base = state_root().resolve()
        candidate = Path(str(raw)).resolve()
        candidate.relative_to(base)
        if candidate.parent != base or not candidate.name.startswith("snap_"):
            return None
        return candidate
    except (OSError, ValueError):
        return None


def cleanup_stale_instances() -> None:
    base = state_root()
    if not base.exists():
        return

    referenced_dirs: set[Path] = set()
    for marker in base.glob("instance_*.json"):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
            created = int(data.get("creation_time", 0))
            instance_dir = safe_instance_dir(data.get("instance_dir", ""))
            if instance_dir is not None:
                referenced_dirs.add(instance_dir)
            if process_matches(pid, created):
                continue
            if instance_dir is not None:
                cleanup_path(instance_dir)
            cleanup_path(marker)
        except Exception:
            cleanup_path(marker)

    # Remove orphan temp directories that no live marker references.
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("snap_") and child not in referenced_dirs:
            cleanup_path(child)


def create_instance_marker(instance_dir: Path, zip_path: Path) -> Path:
    base = state_root()
    base.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    created = current_process_creation_time()
    marker = base / f"instance_{pid}_{created}.json"
    payload = {
        "pid": pid,
        "creation_time": created,
        "instance_dir": str(instance_dir),
        "zip_path": str(zip_path),
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return marker


def copy_file_to_windows_clipboard(path: Path) -> int:
    """Put one file on the Windows clipboard as CF_HDROP and return clipboard sequence."""
    if os.name != "nt":
        raise OSError("Windows clipboard required")

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_int
    user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32

    absolute = str(path.resolve())
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    payload = header + absolute.encode("utf-16le") + b"\x00\x00\x00\x00"

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        raise MemoryError("GlobalAlloc failed")

    clipboard_open = False
    ownership_transferred = False
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("GlobalLock failed")
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)

        for _ in range(20):
            if user32.OpenClipboard(None):
                clipboard_open = True
                break
            time.sleep(0.025)
        if not clipboard_open:
            raise OSError("OpenClipboard failed")
        if not user32.EmptyClipboard():
            raise OSError("EmptyClipboard failed")
        if not user32.SetClipboardData(CF_HDROP, handle):
            raise OSError("SetClipboardData failed")
        ownership_transferred = True
    finally:
        if clipboard_open:
            user32.CloseClipboard()
        if not ownership_transferred:
            kernel32.GlobalFree(handle)

    return int(user32.GetClipboardSequenceNumber())


def clipboard_sequence() -> int:
    if os.name != "nt":
        return -1
    user32 = ctypes.windll.user32
    user32.GetClipboardSequenceNumber.restype = ctypes.c_uint32
    return int(user32.GetClipboardSequenceNumber())


def flash_status(text: str, title: str = "SmartSnap") -> None:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
        win.title(title)
        win.resizable(False, False)
        win.attributes("-topmost", True)
        try:
            win.overrideredirect(True)
        except tk.TclError:
            pass

        width = 560
        height = 240
        bg = "#1e6864"
        win.configure(bg=bg)
        label = tk.Label(
            win,
            text=text,
            font=("Segoe UI", 46, "bold"),
            fg="white",
            bg=bg,
        )
        label.pack(fill="both", expand=True)

        x = max(0, (win.winfo_screenwidth() - width) // 2)
        y = max(0, (win.winfo_screenheight() - height) // 2)
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.deiconify()
        win.lift()
        win.after(500, root.destroy)
        root.mainloop()
    except Exception:
        pass


def wait_until_clipboard_changes(sequence: int) -> None:
    # Keep the backing ZIP alive while CF_HDROP references it.
    while clipboard_sequence() == sequence:
        time.sleep(0.20)


def main() -> None:
    marker: Path | None = None
    instance_dir: Path | None = None
    status = "FAIL"

    try:
        if os.name != "nt":
            raise OSError("SmartSnap is Windows-only")

        cleanup_stale_instances()
        root = Path(__file__).resolve().parent
        base = state_root()
        base.mkdir(parents=True, exist_ok=True)
        instance_dir = Path(tempfile.mkdtemp(prefix="snap_", dir=base))
        zip_path = build_zip(root, instance_dir / OUTPUT_NAME)
        marker = create_instance_marker(instance_dir, zip_path)
        sequence = copy_file_to_windows_clipboard(zip_path)
        status = "OK"
        flash_status(status)

        # Hidden lifetime: the clipboard points to a physical file. Deleting it
        # immediately would make Ctrl+V fail. Delete it as soon as the clipboard
        # is replaced, or let SmartSnapKill.pyw force cleanup.
        wait_until_clipboard_changes(sequence)

    except Exception:
        status = "FAIL"
        if marker is not None:
            cleanup_path(marker)
        if instance_dir is not None:
            cleanup_path(instance_dir)
        flash_status(status)
        return
    finally:
        if status == "OK":
            if instance_dir is not None:
                cleanup_path(instance_dir)
            if marker is not None:
                cleanup_path(marker)


if __name__ == "__main__":
    main()
