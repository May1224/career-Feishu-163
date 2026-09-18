# Security policy

This project reads recruitment emails locally and may store their contents in `data/state.sqlite3`.

- Do not commit `config.json`, `data/`, QR images, mailbox exports, or database files.
- On Windows, the 163 IMAP authorization code is stored in Windows Credential Manager. In GitHub Actions it is supplied through repository Secrets. Do not place either value in source code, issue reports, logs, or screenshots.
- Feishu access may use a locally authenticated `lark-cli` or a self-built app supplied through GitHub Secrets; no token or app secret should be committed.
- Before publishing, run `git status --ignored` and confirm that local configuration and runtime data are excluded.

Please report a suspected security issue privately to the repository maintainer. Do not include credentials or unredacted email content in the report.
