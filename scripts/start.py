import os
import subprocess
import sys


def main() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True, shell=False)  # noqa: S603 - fixed interpreter and arguments
    from app.services.finding_reconciliation import reconcile_profile_duplicates

    reconciled = reconcile_profile_duplicates()
    if reconciled:
        print(f"Reconciled {reconciled} duplicate profile finding(s).", flush=True)
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            os.environ.get("CORVUS_LISTEN_HOST", "127.0.0.1"),
            "--port",
            os.environ.get("CORVUS_LISTEN_PORT", "8000"),
            "--no-access-log",
        ],
    )


if __name__ == "__main__":
    main()
