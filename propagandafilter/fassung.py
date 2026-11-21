"""Fassung aus der einzigen Quelle, version.json im Wurzelordner des Repos."""

import json
from functools import cache
from pathlib import Path

WURZEL = Path(__file__).resolve().parent.parent
NUMMER: str = json.loads((WURZEL / "version.json").read_text())["version"]


@cache
def fassung() -> str:
    return json.loads((WURZEL / "version.json").read_text())["voll"]
