# Updates and rollback

The default upstream is https://github.com/sn0ba11/corvus. The updater checks the source repository’s `main` branch and accepts fast-forward Git history. It stops on tracked local edits or diverged history. A source address explicitly configured for an independent fork is saved as its checkout’s origin.

- Windows package: open **Corvus Update** in the Start menu. The installer can register a daily update task under the installing user.
- macOS package: use the installed Corvus Update launcher. The installer registers a per-user LaunchAgent for daily checks.
- Linux/source: run `python3 Update-Corvus.py --show-source`, then `python3 Update-Corvus.py --check` or `python3 Update-Corvus.py`.

Windows can finish installation even if Task Scheduler denies registration of the daily update task. This was observed with a limited account over SSH on Windows 11. The **Corvus Update** shortcut remains available for manual updates. To retry scheduling from an interactive PowerShell session under the installing account, run the following from the installed Corvus checkout. The task runs with limited privileges under that account.

```powershell
. .\Register-OSINTDailyUpdate.ps1
Register-OSINTDailyUpdate -Root $PWD.Path
```

Verify downloads against the published SHA-256 before running them. The checksum detects corruption or mismatched files and shares the trust of its publication channel. Git update transport uses the configured origin and Git object verification. Packages do not currently include a publisher signature.

Back up case data before updating. For Compose image rollback, run `python3 scripts/installer.py rollback` or `.\OSINTWorkbench.ps1 -Command rollback` on Windows. Database changes require a matching backup restore. Keep the backup private.
