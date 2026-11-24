# Security policy

## Report a vulnerability

Use **Security → Advisories → Report a vulnerability** in the GitHub
repository to send a private report. If the button is unavailable, open an
issue asking how to reach the maintainers privately and keep the technical
details out of that issue.

Include the nimbo version (from `backup --version`), the operating system, the
affected command or action, and the expected and observed behavior. Use
synthetic paths and a disposable destination to describe reproduction steps.
Remove account details, OAuth credentials, tokens, Drive IDs, and private
file names from attachments or examples. Coordinate public disclosure with
the maintainers.

## Credentials and recovery

Keep downloaded OAuth client JSON and local configuration files private.
The refresh token is stored in the local Secret Service keyring. If you
suspect unauthorized access, revoke the application's access from
[Google Account permissions](https://myaccount.google.com/permissions).
Use `backup conectar` to authorize again once the account is secured.

Read the [architecture](docs/architecture.md) for the write and restore
boundaries and the [privacy policy](site/privacy.html) for how nimbo uses
Google account data.
