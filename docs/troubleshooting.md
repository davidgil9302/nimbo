# Troubleshooting

[Back to the README](../README.md)

Start with `backup diagnostico`. It prints each check and a hint when a check
fails. Exit status 1 means at least one critical check failed. Timer
availability, the next run, the last result, and the version are informational
checks.

| What you see | What to check or do |
| --- | --- |
| Configuration is missing or invalid | Run `backup instalar` for a new setup. For an existing setup, check the TOML's section names, required values, and types against [Configuration](configuration.md). |
| Secret Service is unavailable | Open or unlock the Linux keyring and run diagnostics in the same user session as nimbo. Check that it provides the Secret Service interface. |
| Google authorization needs attention | Confirm the OAuth app is **In production**, then run `backup conectar`. If you replaced the desktop client, use `backup conectar --client-json "PATH"` with the private JSON path. See the [OAuth guide](oauth-client.md). |
| The Drive API is unreachable | Check connectivity to Google and make sure **Google Drive API** is enabled in the same Cloud project as the desktop client. |
| The Drive destination is unavailable | Open the folder in Drive with the authorized account. Confirm that the folder exists, is outside the trash, and is writable. Use `backup instalar` to select another existing destination. |
| Source or marker check failed | Mount the correct volume, confirm the configured root, and check for the original `.nimbo-root` marker there. Keep the marker with the source it identifies. |
| An active folder is missing, unreadable, or empty | Restore access or contents, or pause that folder in the dashboard settings if it should be left out of the backup. |
| The next run is missing | Check that the systemd user manager is running and that the timer is enabled. Run `backup instalar` to set up the schedule. A manual setup can use `backup ahora --aqui`. |
| SQLite integrity or local free space check failed | Check the state directory permissions and available space. Preserve the database before investigating it. The database holds the journal and undo records. |
| A backup is waiting for confirmation | Review the list with `backup ahora --confirmar`. If the changes are unexpected, check the source and run `backup pendientes` before approving. |
| Drive storage quota exceeded | Free space or add storage, then start a new run with `backup ahora`. |
| Daily upload limit reached | Uploads pause and pending files can continue in a later run when Google allows uploads again. |
| The engine reports that a run is already active | Check `backup estado`. Use `backup detener` for a service run or Ctrl+C in the terminal running a foreground backup. |
| The mirror differs from the source | Run `backup verificar` to inspect differences. Use `backup verificar --reparar` to run the normal repair plan with safety checks. |

For more detail, run `backup historial` and `backup registro --n 40`. The latter
prints the latest local structured log records. The following systemd
commands show the timer and service journal in a systemd session.

```sh
systemctl --user status nimbo-backup.timer
journalctl --user -u nimbo-backup.service -n 50
```

Before sharing diagnostic output or logs, remove account details, file names,
local paths, and Drive IDs.
