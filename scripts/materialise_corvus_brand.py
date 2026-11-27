#!/usr/bin/env python3
"""Materialise the approved Corvus raster artwork during container builds."""

from __future__ import annotations

import base64
from pathlib import Path

SOURCE = Path("app/static/corvus-mark.jpg.b64")
DESTINATION = Path("app/static/corvus-mark.jpg")


def main() -> None:
    payload = base64.b64decode(SOURCE.read_text(encoding="ascii").strip(), validate=True)
    if len(payload) < 1024 or not payload.startswith(b"\xff\xd8\xff"):
        raise RuntimeError("approved Corvus artwork is not a valid JPEG")
    DESTINATION.write_bytes(payload)


if __name__ == "__main__":
    main()
