"""Apply Corvus's version-specific Debian exceptions to a Trivy JSON image report."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Canonical Corvus image name including its tag")
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "security" / "debian-exceptions.json").read_text(encoding="utf-8")
    )
    today = datetime.now(UTC).date().isoformat()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    family = report["Metadata"].get("OS", {}).get("Family")
    remaining = 0
    accepted = 0
    for result in report["Results"]:
        for finding in result.get("Vulnerabilities", []):
            advisory = finding["VulnerabilityID"]
            package = finding["PkgName"]
            version = finding["InstalledVersion"]
            exception = policy.get(advisory)
            if (
                family == "debian"
                and result["Type"] == "debian"
                and finding["Severity"] == "HIGH"
                and not finding.get("FixedVersion")
                and exception
                and today < exception["expires"]
                and version == exception["packages"].get(package)
                and args.image in exception["images"]
            ):
                accepted += 1
            else:
                print(f"{args.image}: {advisory} {package} {version} ({finding['Severity']})")
                remaining += 1
        for finding in result.get("Secrets", []):
            print(f"{args.image}: secret {finding['RuleID']} in {result['Target']}:{finding['StartLine']}")
            remaining += 1
    print(f"{args.image}: {accepted} accepted Debian findings, {remaining} blocking findings")
    return int(remaining > 0)


if __name__ == "__main__":
    raise SystemExit(main())
