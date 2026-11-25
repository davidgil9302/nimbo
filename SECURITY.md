# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| Older | No |

## Reporting a vulnerability

Please report security problems privately. Open the repository's **Security**
tab and select **Report a vulnerability** to use GitHub's private
vulnerability reporting. Do not open a public issue, discussion, or pull
request for a security problem. If that option is unavailable, open an issue
that only asks how to reach the maintainers privately, with no technical
details.

Include:

- the nimbo version (`backup --version`) and your Linux distribution;
- the command or dashboard action involved;
- what you expected and what happened;
- reproduction steps, using synthetic paths and a disposable Drive folder.

Remove account details, OAuth client credentials, tokens, Drive IDs, and
private file names from reports and attachments.

Every report gets an acknowledgement. You will then receive either a fix or an
explanation of why the behavior is not considered a vulnerability. Please
coordinate public disclosure with the maintainers.

## Threat model

nimbo is a one-way mirror: it copies selected folders from a Linux machine to
an existing Google Drive folder. This section describes what it relies on and
the protections the code enforces.

### What nimbo trusts

- The local user account it runs as, and the operating system.
- Google's OAuth and Drive APIs, reached over HTTPS.
- The desktop OAuth client that you create and configure.

### Google access

- **Scope.** nimbo requests the full Google Drive scope
  (`https://www.googleapis.com/auth/drive`) and rejects a sign-in whose grant
  does not include it. It needs this scope to manage the mirror inside an
  existing folder that you choose and to work with items its OAuth client did
  not create.
- **Sign-in.** The OAuth redirect goes to a temporary listener on
  `127.0.0.1`. The flow uses PKCE (S256) and a random `state` value that the
  callback must match. The browser is launched with a single-use local start
  link, so the `state` value and PKCE challenge never appear in a process's
  arguments. A second request for the start link, or a callback that arrives
  before the start link was served, aborts the sign-in. The listener caps
  concurrent connections, and the sign-in times out after 5 minutes.
- **Account confirmation.** During interactive setup (`backup instalar`),
  nimbo shows which Google account answered and stores the new credential only
  after you confirm it. `backup conectar` asks the same question when it runs
  in a terminal.
- **Account binding.** The journal records the account for the mirror.
  `backup conectar` refuses an account other than the recorded one and keeps
  the existing credential. Switching accounts requires `backup instalar`,
  which resets the journal for the new account. A backup run checks the
  signed-in account before it writes anything and stops if it has changed.

### Where secrets and state live

- **Refresh credential.** The OAuth refresh token is stored in the desktop
  keyring through the Secret Service API, never in a plain file. Each
  configuration directory is a separate profile with its own keyring item.
  Lookups, replacements, and deletions are limited to that profile's item.
- **Files nimbo writes.** Configuration, state, and log directories are
  created with mode 0700, and existing directories with broader permissions
  are tightened.

  | Path (defaults) | Contents | Mode |
  | --- | --- | --- |
  | `~/.config/nimbo/config.toml` | Settings and the OAuth desktop client ID and secret | 0600 |
  | `~/.config/nimbo/` launcher | Command used by the systemd service, if installed | 0700 |
  | `~/.local/state/nimbo/nimbo.db` | Journal: file paths, Drive IDs, run history, resumable upload sessions | 0600 |
  | `~/.local/state/nimbo/` (`nimbo.lock` and JSON state files) | Lock, progress, and approval state | 0600 |
  | `~/.local/state/nimbo/logs/` | Daily JSONL logs, kept for 30 days | 0600 |

- **Logs.** Before a record is written, nimbo redacts access tokens, refresh
  tokens, the client secret, authorization codes, upload session IDs, and
  `Bearer` headers. Redaction is applied to the message, structured fields,
  and tracebacks. A record that cannot be redacted safely is dropped.

### Drive boundary

- nimbo never modifies, moves, or trashes items it does not own. These
  include items owned by another Google account, items marked as belonging to
  a different nimbo mirror, Google-native files (Docs, Sheets, Slides), and
  shortcuts. nimbo also leaves excluded and protected paths, and items outside
  the selected folders, unchanged.
- Mirror items carry a Drive property with this mirror's identifier. Writes
  are checked against the current Drive state, including parent links, so
  they stay inside the configured target folder.
- Inside the selected folders of the target, nimbo treats files owned by the
  signed-in account that have no nimbo property as part of the mirror. It may
  adopt matching files, and it can move files with no local counterpart to
  the trash, subject to the brakes below. Do not keep unrelated files of your
  own inside those folders.

### Deletion and overwrite safety

- **Trash, not deletion.** nimbo removes mirror items by moving them to the
  Drive trash. It never deletes Drive items permanently.
- **Deletion brake.** By default, a plan waits for review if it would trash
  more than 500 files, or more than 10% of a top-level folder when at least
  20 files there would be trashed.
- **Overwrite brake.** By default, a plan waits for review if it would
  overwrite more than 1000 files, or more than 50% of a top-level folder when
  at least 50 files there would be overwritten.
- **Approvals.** An approval is bound to a fingerprint of the reviewed plan
  and is valid only for that plan. It expires after 24 hours and can be used
  only once.
- **Undo.** `backup deshacer` restores items from the Drive trash after
  checking that they are inside the target folder and not owned by another
  mirror. It writes a local file only when all of the following are true:
  nimbo trashed the file because it had been deleted locally, the file
  belonged to this mirror, and its size and MD5 checksum match what nimbo
  had backed up. The download is checked again before it is published. Undo
  never replaces existing local paths.
- **Restored file modes.** Restored files are created with mode 0600 and new
  directories with mode 0700, whatever the umask. Original permissions are
  not restored.

### Local files

- nimbo reads only the selected folders under the source root. It respects
  exclusions and `.nobackup` markers.
- It does not follow symbolic links. Links are reported and skipped. Files
  are opened without following links, and nimbo checks that the opened file
  is still the one it scanned.
- Before a run, nimbo checks the `.nimbo-root` marker in the source. If that
  folder is missing, unreadable, or empty, the run stops.
- Before undo writes anything, nimbo rejects unsafe path components and
  creates paths relative to the source root without following links.

## Known limitations and accepted risks

- **Other local users and the sign-in link.** On Linux, other users can
  usually read process command lines (`/proc/<pid>/cmdline`), including the
  single-use start link passed to the browser launcher. A very fast program
  could open that link first and complete Google's consent with its own
  account before your browser does. In interactive use, the account
  confirmation then shows the wrong account before anything is stored.
  `backup conectar` also refuses an account other than the recorded one.
  Non-interactive setup (`--no-interactivo`), and `backup conectar` outside a
  terminal, skip the confirmation. To harden shared machines, mount `/proc`
  with `hidepid=2` (`hidepid=invisible`), or do not run nimbo on machines with
  untrusted local users.
- **Malware running as your user.** Malware running as your user can read
  the unlocked keyring and any file nimbo can read. This is out of scope.
- **Other writers in Drive.** Any app or collaborator with write access to
  the same Drive can change or delete the mirror there. The mirror is not an
  immutable archive. Use Drive's version history and trash, and keep an
  offline copy of critical data.
- **Google account compromise.** This is out of scope. If you suspect it,
  secure the account and revoke nimbo's access in
  [Google Account permissions](https://myaccount.google.com/permissions).
  Then run `backup conectar` to authorize again.

## Hardening tips

- Lock the keyring, or your session, when you step away.
- Use a dedicated OAuth desktop client for nimbo, and keep its JSON file and
  `config.toml` private.
- Review the output of `backup diagnostico` after setup and after failures.
- Keep the source folder private to your user account.
- Review large deletion and overwrite plans before approving them.

## Related documents

Read the [architecture](docs/architecture.md) for the write and restore
boundaries, and the [privacy policy](site/privacy.html) for how nimbo uses
Google account data.
