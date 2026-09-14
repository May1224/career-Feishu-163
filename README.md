# 163 求职邮件跟踪器

本机只读扫描 163 邮箱中的招聘邮件，由 Codex 归纳求职进展，并同步到飞书多维表格。项目不调用额外模型 API；飞书身份由本机已登录的 `lark-cli` 管理。

## 功能

- IMAP 只读访问，使用 `EXAMINE` 与 `BODY.PEEK`，不改变邮件已读状态。
- 同一公司、岗位和招聘批次分别跟踪；稳定同步键防止飞书重复建行。
- 总览只显示公司名、岗位名、流程阶段、阶段截止时间，并按收件日期倒序。
- 一条 `run` 命令完成一次有界读取；没有待分析邮件时自动同步飞书。
- 邮件正文保留在本机 SQLite 数据库中，不设自动过期时间。

## 运行环境

- Windows 10/11（授权码保存到 Windows Credential Manager）
- Python 3.10+
- 已安装并登录、且拥有飞书多维表格读写权限的 `lark-cli`
- 已启用 IMAP 的 163 邮箱及客户端授权码

## 本机配置

1. 复制 `config.example.json` 为 `config.json`，或运行 `配置邮箱和飞书.cmd`。
2. 在 163 邮箱开启 IMAP，并将客户端授权码填入本机配置窗口。
3. 登录 `lark-cli`，授权目标飞书账号访问多维表格。
4. 运行以下命令完成连通性和建表验证：

```powershell
python -m career_tracker doctor
python -m career_tracker init-feishu
```

首次扫描默认从所有可选收件文件夹读取，并按 `--max-new` 分批推进。需要限制起始时间时，可在 `config.json` 中设置 `initial_since`，格式为 `01-Jan-2026`。

## 日常运行

```powershell
python -m career_tracker run --max-new 200
```

如果输出包含 `pending_messages`，依次执行 `prepare`、让 Codex 按 [WORKFLOW.md](WORKFLOW.md) 分析 `data/pending.json`、再执行 `ingest`。随后再次运行 `run`。当输出中的 `fetch.more` 为 `true` 时，继续循环即可。

```powershell
python -m career_tracker prepare --limit 20
python -m career_tracker ingest data/result.json
python -m unittest discover -s tests -v
```

完整的 JSON 契约、定时任务步骤和判断规则见 [WORKFLOW.md](WORKFLOW.md)。

## 数据与安全

运行数据位于 `data/`，配置位于 `config.json`，均被 Git 忽略。不要提交数据库、邮件正文、二维码、IMAP 授权码或飞书访问凭据。发布前请阅读 [SECURITY.md](SECURITY.md)。
