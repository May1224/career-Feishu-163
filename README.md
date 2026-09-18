# 163 求职邮件跟踪器

只读扫描 163 邮箱中的招聘邮件，并同步到飞书多维表格。既可在本机由 Codex 辅助分析，也可由 GitHub Actions 每日无人值守运行。

## 功能

- IMAP 只读访问，使用 `EXAMINE` 与 `BODY.PEEK`，不改变邮件已读状态。
- 同一公司、岗位和招聘批次分别跟踪；稳定同步键防止飞书重复建行。
- 总览只显示公司名、岗位名、流程阶段、阶段截止时间，并按收件日期倒序。
- 一条 `run` 命令完成一次有界读取；没有待分析邮件时自动同步飞书。
- GitHub Actions 运行时将 IMAP 游标保存在飞书的“系统状态”表中；不依赖 Actions 的临时磁盘。
- 没有模型 API Key 时，使用保守规则识别明确的投递、测评、笔试、面试和淘汰邮件；不确定内容进入待确认。

## 运行环境

- Windows 10/11（授权码保存到 Windows Credential Manager）
- Python 3.10+
- 已安装并登录、且拥有飞书多维表格读写权限的 `lark-cli`
- 已启用 IMAP 的 163 邮箱及客户端授权码

## GitHub Actions 无人值守运行

1. 在飞书开放平台创建并发布自建应用，授予目标 Base 的读写权限，并把应用加入该 Base 的可编辑协作者。
2. 在 GitHub 仓库 `Settings → Secrets and variables → Actions` 配置：`MAIL_ADDRESS`、`MAIL_AUTH_CODE`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_APP_TOKEN`、`FEISHU_NOTIFY_WEBHOOK`。
3. 已有表格内容时，首次手动运行选择 `bootstrap`。它只保存每个文件夹当前的 IMAP 游标，不读取或写入历史应聘记录。之后定时任务只读取新增邮件；需要手动补跑时选择 `sync`。

工作流每天北京时间约 20:07 运行。飞书会新增“系统状态”表，仅保存 IMAP 游标；应聘总览仍维持四个展示字段。运行日志只输出汇总，不输出邮件正文或密钥。

可选的模型分析使用 OpenAI 兼容接口。新增 `LLM_API_KEY` Secret，并在 GitHub Variables 设置 `LLM_BASE_URL`、`LLM_MODEL` 与 `LLM_PROVIDER`。例如 Agnes 使用 `LLM_BASE_URL=https://apihub.agnes-ai.com/v1`，并将 `LLM_PROVIDER` 设为 `agnes`；模型名以 Agnes 控制台显示的文本模型标识为准。

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
