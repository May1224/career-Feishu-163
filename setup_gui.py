"""Run locally; masked secret fields write directly to Windows Credential Manager."""
import json
import re
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlparse

from career_tracker import credentials
from career_tracker.core import ROOT, connect, save_json, set_meta


def main():
    path = ROOT / 'config.json'
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    root = tk.Tk()
    root.title('求职邮件跟踪 · 本机配置')
    root.geometry('740x405')
    root.resizable(False, False)
    panel = ttk.Frame(root, padding=20)
    panel.pack(fill='both', expand=True)
    ttk.Label(panel, text='凭据直接保存到 Windows 凭据管理器，不会发送到聊天。').grid(row=0, column=0, columnspan=2, sticky='w', pady=10)
    definitions = [('email', '163 邮箱地址', False), ('imap_authorization', '163 客户端授权码', True)]
    entries = {}
    for row, (key, label, secret) in enumerate(definitions, 1):
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky='w', pady=10)
        entry = ttk.Entry(panel, width=58, show='*' if secret else '')
        entry.grid(row=row, column=1, padx=10)
        if not secret:
            entry.insert(0, old.get(key, ''))
        entries[key] = entry
    ttk.Label(panel, text='飞书连接由已登录的 lark-cli 管理，无需在此填 App Secret。\n已有授权码可留空保留；更换邮箱请使用独立项目，避免混入旧数据。').grid(row=3, column=0, columnspan=2, sticky='w', pady=10)

    def save():
        values = {k: e.get().strip() for k, e in entries.items()}
        if not re.fullmatch(r'[^\s@]+@163\.com', values['email'], re.I):
            messagebox.showerror('配置错误', '请输入有效的 @163.com 邮箱地址。')
            return
        if old and old.get('email') != values['email']:
            messagebox.showerror('配置错误', '当前项目已绑定另一邮箱；请使用独立项目。')
            return
        try:
            if values['imap_authorization']:
                credentials.put('imap_authorization', values['imap_authorization'])
            else:
                credentials.get('imap_authorization')
            settings = dict(old, email=values['email'], backend='cli', timezone='Asia/Shanghai')
            save_json(path, settings)
            db = connect()
            with db:
                set_meta(db, 'ready', False)
            db.close()
            entries['imap_authorization'].delete(0, 'end')
            messagebox.showinfo('配置已保存', '已保存到本机。回到 Codex 告知“配置好了”，即可继续联调和启用定时任务。')
            root.destroy()
        except Exception:
            messagebox.showerror('保存失败', '请确认当前 Windows 账户能够使用凭据管理器；未输出敏感信息。')

    ttk.Button(panel, text='保存到本机', command=save).grid(row=4, column=1, sticky='e', pady=8)
    root.mainloop()


if __name__ == '__main__':
    main()
