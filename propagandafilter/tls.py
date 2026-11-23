"""Mit welcher Zertifikatsstelle die Dienste im Haus geprüft werden."""

from __future__ import annotations

import ssl
from pathlib import Path


def pruefung(stelle: Path | None) -> ssl.SSLContext | bool:
    if stelle is None:
        return True
    datei = stelle.expanduser()
    if not datei.is_file():
        raise OSError(f"Zertifikatsstelle {datei} fehlt")
    return ssl.create_default_context(cafile=str(datei))
