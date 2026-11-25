# Architecture

[Back to the README](../README.md)

A backup reconciles a selected set of local folders with a subtree in Google
Drive. Local paths and contents drive the plan. The engine owns backup writes,
and the command-line interface and Textual dashboard expose the engine and
the systemd user service.

## A backup run

1. **Check the source and destination.** The engine takes an exclusive
   filesystem lock. It checks the source marker, the mount when required,
   and the active source folders. It checks connectivity, obtains Drive
   account and quota information, and verifies that the target is available.
2. **Reconcile and refresh.** The engine examines any unfinished journaled
   operations by Drive file ID. It refreshes the SQLite remote index from a
   full subtree listing or the Drive account's changes feed. The indexer
   filters feed metadata locally to the target subtree before storing it.
   The index and changes cursor advance together. The indexer can fall back
   to a full listing when the cursor is stale or the feed proves unreliable.
3. **Scan and plan.** The scanner walks active folders, normalizes relative
   names to Unicode NFC, and records paths that need protection. A local hash
   cache helps avoid repeat reads. The planner consumes the local scan and
   indexed Drive tree without performing Drive writes.
4. **Apply preflight limits.** The engine checks deletion brakes and
   available Drive storage before applying the plan. A brake approval is
   single-use, expires after 24 hours, and matches a fingerprint of the
   reviewed plan.
5. **Journal and execute.** The engine allocates Drive IDs for new entries
   and saves the ordered plan in SQLite before applying it. The executor
   records intent before each write. It creates parents before children,
   handles moves ahead of dependent uploads, and processes trash operations
   last. It records outcomes and the IDs needed for undo.
6. **Recover after interruption.** On a later run, the engine reconciles
   unfinished writes and can reuse generated IDs and valid upload sessions.
   A resumable upload probes Google's confirmed offset before continuing.
   Uploads are verified against Drive's MD5, and files that change mid-upload
   can be retried.

## Planner and write boundaries

The planner can recognize matching files already in Drive and adopt them
without re-uploading. It can move matching files and eligible folders
server-side. It protects excluded paths, Google-native files, shortcuts, and
items owned by another account or marked as belonging to another mirror. When
an unowned or Google-native item shares a local name, nimbo backs up to its own
file or folder with that name. For folders, nimbo backs up the subtree under
its own folder ID. The executor checks subtree membership against the index
and uses live Drive ancestry checks for operations such as trashing and uploading.

Deletes use Drive's trash flag. Undo uses the recorded Drive IDs to untrash
eligible items and restore missing local data without overwriting existing
paths. Verification compares freshly hashed local files with a full Drive
listing. Optional repair uses the usual backup planner and safety checks.

## Local state and the dashboard

SQLite runs in WAL mode with full synchronization for writable connections.
The database holds remote entries and the changes cursor, cached local hashes,
run and operation records, resumable upload sessions, trash records, and
per-folder statistics. The engine also publishes progress through
`progress.json` and records structured local logs.

The dashboard reads the index in read-only mode for status and reads
`progress.json` for live transfers. It requests starts and stops through
systemd. Actions such as preview and undo delegate to the engine. The oneshot
`nimbo-backup.service` runs independently of the terminal, and
`nimbo-backup.timer` starts it daily.

OAuth uses a local browser redirect with PKCE and a checked state value.
Refresh credentials live in the local Secret Service keyring. See
[Configuration](configuration.md) for storage paths and defaults and
[Testing](testing.md) for the failure and recovery test suites.
