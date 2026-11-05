"""Prompt files hot-reloaded on change, so non-developers can edit tone without a redeploy."""

from __future__ import annotations

from pathlib import Path


class PromptFile:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime = -1.0
        self._text = ""

    def read(self) -> str:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return self._text
        if mtime != self._mtime:
            self._text = self.path.read_text(encoding="utf-8")
            self._mtime = mtime
        return self._text
