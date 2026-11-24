# Configuration

[Back to the README](../README.md)

Run `backup instalar` to create the configuration and initialize the source
marker. The TOML file is normally `~/.config/nimbo/config.toml` and is saved
with permissions 0600. Keep it private because it includes the OAuth desktop
client credentials.

## Paths

| Path | Contents |
| --- | --- |
| `$XDG_CONFIG_HOME/nimbo/` (default `~/.config/nimbo/`) | `config.toml` and the service launcher, when installed. |
| `$XDG_STATE_HOME/nimbo/` (default `~/.local/state/nimbo/`) | `nimbo.db`, `nimbo.lock`, `progress.json`, `approval.json`, `request.json`, and JSONL logs under `logs/`. |
| `$XDG_CONFIG_HOME/systemd/user/` (default `~/.config/systemd/user/`) | `nimbo-backup.service` and `nimbo-backup.timer`. |
| The source root | `.nimbo-root`, the marker that identifies the source. |
| The local Secret Service keyring | The OAuth refresh token. |

`NIMBO_CONFIG_DIR` and `NIMBO_STATE_DIR` override the corresponding nimbo
directories. The launcher for a scheduled installation captures those paths
during setup. The systemd user-unit location follows `XDG_CONFIG_HOME`.

## TOML example

Replace the placeholders in angle brackets with values from your setup.
The wizard accepts a Drive folder link or ID. In TOML, use the bare ID.

```toml
schema = 1
service = "Google Drive"
language = "en"
exclude_ignore_case = true

[source]
root = "/media/alice/DATA"
folders = ["Projects", "Photos"]
paused = []
require_mount = true

[target]
folder_id = "<Drive folder ID>"
label = "Backup mirror"

[schedule]
daily_at = "20:00"

[safety]
max_deletes = 500
max_delete_ratio = 0.10
ratio_min_files = 20

[transfer]
parallel_uploads = 4
chunk_mib = 32
multipart_max_mib = 8
writes_per_second = 3.0
reads_per_second = 10.0

[oauth]
client_id = "<Desktop client ID>"
client_secret = "<Desktop client secret>"
```

## Keys and defaults

| Key | Default | Meaning |
| --- | --- | --- |
| `schema` | `1` | Configuration format version. Version 1 is required. |
| `service` | `"Google Drive"` | Service name displayed in the interface. |
| `language` | Omitted | `"es"` or `"en"`. If omitted, nimbo detects the language from the locale and falls back to English. |
| `exclude` | See below | Root-level array of exclusion globs. Setting it replaces the list below. |
| `exclude_ignore_case` | `true` | Match exclusion globs case-insensitively. |
| `source.root` | Required | Absolute path to the local source root. |
| `source.folders` | Required | A nonempty list of distinct, immediate subfolder names. |
| `source.paused` | `[]` | Selected folder names to leave out of backup runs. |
| `source.require_mount` | `true` | Require the source root itself to be a mount point. For a new root, the wizard sets this based on whether it is a mount point during setup. |
| `target.folder_id` | Required | ID of the existing Drive destination folder. |
| `target.label` | `""` | Optional display name. The wizard fills it from Drive. |
| `schedule.daily_at` | `"20:00"` | Daily start time in local 24-hour `HH:MM` format. |
| `safety.max_deletes` | `500` | Ask for confirmation when the plan would trash more than this many files in total. |
| `safety.max_delete_ratio` | `0.10` | Ask for confirmation when the share of a folder's indexed files to be trashed exceeds this fraction and meets the minimum count below. Valid values are greater than 0 and at most 1. |
| `safety.ratio_min_files` | `20` | Minimum number of deletions in a folder before its deletion ratio is checked. |
| `transfer.parallel_uploads` | `4` | Concurrent uploads, from 1 to 16. |
| `transfer.chunk_mib` | `32` | Resumable chunk size in MiB, from 1 to 256. |
| `transfer.multipart_max_mib` | `8` | Multipart size threshold in MiB, from 1 through `chunk_mib`. Set this explicitly when lowering the chunk size below 8 MiB. |
| `transfer.writes_per_second` | `3.0` | Drive write rate, from 0.1 to 20 requests per second. |
| `transfer.reads_per_second` | `10.0` | Drive read rate, from 0.1 to 50 requests per second. |
| `oauth.client_id` | Required | Desktop client ID issued by Google. |
| `oauth.client_secret` | Required | Desktop client secret issued by Google. |

## Exclusions

The following is the default `exclude` array. In TOML it belongs above the
first table header.

```toml
exclude = [
  "node_modules/",
  "__pycache__/",
  ".venv/",
  ".ipynb_checkpoints/",
  ".mypy_cache/",
  ".pytest_cache/",
  ".ruff_cache/",
  ".tmp.drivedownload/",
  ".tmp.driveupload/",
  ".Trash-*/",
  "desktop.ini",
  "Thumbs.db",
  ".DS_Store",
  "~$*",
  "*.crdownload",
]
```

A pattern without a slash matches a basename at any level. A leading slash
anchors a pattern to the source root. A trailing slash matches directories,
including their descendants. Use `**` between path components to match
multiple levels, as in `Projects/**/cache/`. Empty patterns and patterns
starting with `!` or `#` are invalid. Write TOML comments outside the quoted
patterns.

nimbo also excludes `.nimbo-root` and `*.nimbo-part`. A directory containing a
regular file named `.nobackup` is skipped and its Drive subtree is protected.
Preview changes after editing exclusions with `backup pendientes`.

## systemd user timer

Change the scheduled time through the dashboard settings or the setup wizard
so the installed timer is updated as well as the TOML. The
`nimbo-backup.timer` unit uses the local time in `OnCalendar`, a randomized
delay of up to five minutes, and `Persistent=true` to catch up after a missed
schedule when the timer next starts.

The `nimbo-backup.service` unit is a oneshot that runs the installed command
via the launcher with `ejecutar --auto`. It uses `Nice=10`, idle I/O
scheduling, and a 120-second stop timeout. The user manager and access to the
unlocked keyring must be available when a scheduled run starts. To set up
without units, use `backup instalar --sin-servicio` and run backups manually
with `backup ahora --aqui`.
