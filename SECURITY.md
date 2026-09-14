# Security policy

This project reads recruitment emails locally and may store their contents in `data/state.sqlite3`.

- Do not commit `config.json`, `data/`, QR images, mailbox exports, or database files.
- The 163 IMAP authorization code is stored in Windows Credential Manager. Do not place it in source code, issue reports, logs, or screenshots.
- Feishu access is delegated to the locally authenticated `lark-cli`; no Feishu token should be committed.
- Before publishing, run `git status --ignored` and confirm that local configuration and runtime data are excluded.

Please report a suspected security issue privately to the repository maintainer. Do not include credentials or unredacted email content in the report.
