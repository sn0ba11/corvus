"""Manual harmless smoke checks. Never invoked by pytest or CI."""

import argparse
import json
import urllib.parse
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--identifier", default="github")
    args = parser.parse_args()
    with urllib.request.urlopen(f"{args.base_url}/healthz", timeout=5) as response:
        print("health:", json.load(response))
    with urllib.request.urlopen(
        f"{args.base_url}/api/detect?q={urllib.parse.quote(args.identifier)}", timeout=5
    ) as response:
        print("detection:", json.load(response))
    print(
        "Live searches are intentionally not submitted automatically. Use the GUI with an identifier you control."
    )


if __name__ == "__main__":
    main()
