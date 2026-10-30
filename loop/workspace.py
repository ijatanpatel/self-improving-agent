"""File tools for the agents, locked inside one repository folder.

Agents never get a shell. They get these functions instead, and every path is checked:
it must stay inside the repo, secrets like .env cannot be read, and edits are only
allowed under an explicit allowlist of folders.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from loop import settings

MAX_READ_LINES = 250
MAX_SEARCH_HITS = 50


class Workspace:
    def __init__(self, root: Path, editable: tuple[str, ...] = (), protected: tuple[str, ...] = ()):
        self.root = root.resolve()
        self.editable = editable
        self.protected = protected
        self.changed: set[str] = set()

    # ---- path checks -------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root):
            raise PermissionError(f"{path} is outside the repository")
        rel = full.relative_to(self.root)
        if set(rel.parts) & settings.SKIPPED_DIRS or rel.name in settings.SECRET_FILES:
            raise PermissionError(f"{rel.as_posix()} is off-limits to agents")
        return full

    def rel(self, full: Path) -> str:
        return full.relative_to(self.root).as_posix()

    def check_editable(self, path: str) -> Path:
        full = self.resolve(path)
        rel = self.rel(full)
        if not rel.startswith(self.editable) or rel.startswith(self.protected):
            raise PermissionError(
                f"{rel} is read-only for this agent; it may only edit files under "
                f"{', '.join(self.editable)} (except {', '.join(self.protected) or 'nothing'})"
            )
        return full

    def iter_files(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in settings.SKIPPED_DIRS)
            for name in sorted(filenames):
                if name not in settings.SECRET_FILES and not name.endswith((".pyc", ".npy", ".db")):
                    yield Path(dirpath) / name

    # ---- read-only tools -----------------------------------------------------------
    def list_files(self, directory: str = ".") -> str:
        base = self.resolve(directory)
        files = [self.rel(p) for p in self.iter_files() if p.is_relative_to(base)]
        return "\n".join(files[:300]) or "(no files)"

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        full = self.resolve(path)
        if not full.is_file():
            return f"ERROR: {path} is not a file"
        lines = full.read_text(encoding="utf-8").splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:4d}| {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines)" if end < len(lines) else ""
        return f"{self.rel(full)} (lines {start}-{end} of {len(lines)})\n{body}{more}"

    def search_code(self, pattern: str, glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        hits = []
        for path in self.iter_files():
            rel = self.rel(path)
            if glob and not fnmatch.fnmatch(rel, glob):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(lines, start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        return "\n".join(hits) + "\n... (more matches; narrow the pattern)"
        return "\n".join(hits) or "No matches."

    # ---- editing tools (fix agent only) ---------------------------------------------
    def replace_in_file(self, path: str, old_text: str, new_text: str) -> str:
        full = self.check_editable(path)
        content = full.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count != 1:
            return (f"ERROR: old_text appears {count} times in {path}; it must appear exactly once. "
                    "Include more surrounding lines to make it unique.")
        full.write_text(content.replace(old_text, new_text), encoding="utf-8", newline="\n")
        self.changed.add(self.rel(full))
        return f"Edited {self.rel(full)}"

    def create_file(self, path: str, content: str) -> str:
        full = self.check_editable(path)
        if full.exists():
            return f"ERROR: {path} already exists; use replace_in_file to change it"
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8", newline="\n")
        self.changed.add(self.rel(full))
        return f"Created {self.rel(full)}"
