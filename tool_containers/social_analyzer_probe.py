#!/usr/bin/env python3
"""Bounded Social Analyzer wrapper that emits one JSON evidence file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

USERNAME_RE = re.compile(r"^[\w.\-]{1,64}$", re.UNICODE)
OUTPUT_PATH = Path("results.json")
SOCIAL_ANALYZER_TOP = "50"


def serialisable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): serialisable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [serialisable(item) for item in value]
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    username = args.username.strip()
    if not USERNAME_RE.fullmatch(username):
        OUTPUT_PATH.write_text(
            json.dumps({"status": "error", "error": "invalid username"}), encoding="utf-8"
        )
        print("Social Analyzer rejected an invalid username", file=sys.stderr)
        return 2
    try:
        spec = spec_from_file_location("social_analyzer_upstream", "/opt/tool/app.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("Social Analyzer source module is unavailable")
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        analyzer = module.SocialAnalyzer()
        result = analyzer.run_as_object(
            username=username,
            silent=True,
            output="json",
            filter="good",
            method="find",
            metadata=True,
            extract=True,
            timeout=6,
            profiles="detected",
            top=SOCIAL_ANALYZER_TOP,
        )
        payload = serialisable(result)
        OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "completed", "username": username}))
        return 0
    except Exception as exc:  # upstream raises several undocumented exception types
        detail = " ".join(str(exc).split())[:300]
        message = f"Social Analyzer failed: {type(exc).__name__}"
        if detail:
            message = f"{message}: {detail}"
        OUTPUT_PATH.write_text(json.dumps({"status": "error", "error": message}), encoding="utf-8")
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
