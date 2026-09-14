import email
import imaplib
import json
import re
import ssl
from datetime import datetime
from email import policy
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from .core import TZ, digest, get_meta, now, set_meta

KEYWORDS = re.compile(r'招聘|应聘|投递|简历|笔试|面试|测评|校招|实习|录用|补充材料|人才|招聘进展|应届|入职|offer|interview|recruit|application|assessment|career|hiring|resume|candidate', re.I)


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'tr', 'li'):
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def parse_message(raw, internal_date):
    msg = email.message_from_bytes(raw, policy=policy.default)
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = body.get_content(errors='replace') if body else ''
    html_body = msg.get_body(preferencelist=('html',))
    if html_body and len(str(text).strip()) < 100:
        alternative = html_body.get_content(errors='replace')
        parser = PlainHTML()
        parser.feed(alternative)
        expanded = ''.join(parser.text)
        if len(expanded.strip()) > len(str(text).strip()):
            body, text = html_body, alternative
    if not isinstance(text, str):
        text = ''
    if body and body.get_content_type() == 'text/html':
        parser = PlainHTML()
        parser.feed(text)
        text = ''.join(parser.text)
    subject = str(msg.get('Subject', ''))
    sender = str(msg.get('From', ''))
    candidate = bool(KEYWORDS.search(subject + '\n' + sender + '\n' + text))
    try:
        sent = parsedate_to_datetime(str(msg.get('Date', '')))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=TZ)
        sent = sent.astimezone(TZ).isoformat()
    except (ValueError, TypeError, OverflowError):
        sent = internal_date
    attachments = [p.get_filename() or '(未命名附件)' for p in msg.iter_attachments()]
    payload = dict(subject=subject, sender=sender, received=internal_date, sent=sent,
                   body=text[:24000], truncated=len(text) > 24000, attachments=attachments)
    # Identical copies in multiple folders collapse; reused Message-ID with different content does not.
    identity = digest([str(msg.get('Message-ID', '')), subject, sender, sent, text, attachments])
    return identity, payload, candidate


def folder_names(client):
    status, lines = client.list()
    if status != 'OK':
        raise RuntimeError('无法枚举邮箱文件夹')
    result = []
    for line in lines or []:
        if not isinstance(line, bytes):
            raise RuntimeError('邮箱文件夹返回格式不支持，请显式配置 folders')
        match = re.match(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)', line)
        if not match:
            raise RuntimeError('邮箱文件夹返回格式不支持，请显式配置 folders')
        flags, name = match.groups()
        if any(flag in flags.lower().split() for flag in (b'\\noselect', b'\\trash', b'\\junk', b'\\sent', b'\\drafts')):
            continue
        if name.lower().strip(b'"') in (b'trash', b'junk', b'spam', b'sent', b'drafts'):
            continue
        result.append(name.decode('ascii'))  # Preserve server modified UTF-7 and quoting.
    return result


def fetch(db, config, password, max_new=200, factory=imaplib.IMAP4_SSL):
    client = factory('imap.163.com', 993, ssl_context=ssl.create_default_context(), timeout=30)
    count = scanned = 0
    try:
        client.login(config['email'], password)
        # NetEase may require IMAP ID before SELECT.
        if b'ID' in [c.upper() if isinstance(c, bytes) else c.upper().encode() for c in client.capabilities]:
            imaplib.Commands.setdefault('ID', ('AUTH', 'SELECTED'))
            client._simple_command('ID', '("name" "CodexCareerTracker" "version" "1.0")')
        folders = config.get('folders') or folder_names(client)
        for folder in folders:
            status, _ = client.select(folder, readonly=True)
            if status != 'OK':
                raise RuntimeError('无法只读打开邮箱文件夹；请检查 IMAP 客户端授权')
            response = client.response('UIDVALIDITY')[1]
            if not response or not response[0]:
                raise RuntimeError('邮箱未提供 UIDVALIDITY')
            validity = response[0].decode()
            cursor_key = 'cursor:' + folder
            cursor = get_meta(db, cursor_key)
            if cursor and cursor['validity'] == validity:
                criterion = f'UID {cursor["uid"] + 1}:*'
            else:
                anchor = get_meta(db, 'initial_since')
                if anchor is None:
                    anchor = config.get('initial_since')
                    if anchor is not None and not re.fullmatch(r'\d{1,2}-[A-Za-z]{3}-\d{4}', anchor):
                        raise ValueError('initial_since 须为 DD-Mon-YYYY，例如 01-Jan-2026')
                    with db:
                        set_meta(db, 'initial_since', anchor)
                criterion = 'SINCE ' + anchor if anchor else 'ALL'
            status, data = client.uid('SEARCH', None, criterion)
            if status != 'OK':
                raise RuntimeError('邮件检索失败')
            uids = sorted(int(x) for x in (data[0] or b'').split())
            for uid in uids:
                if cursor and cursor['validity'] == validity and uid <= cursor['uid']:
                    continue
                if scanned >= max_new:
                    return dict(candidates=count, scanned=scanned, more=True)
                if db.execute('SELECT 1 FROM seen WHERE folder=? AND validity=? AND uid=?', (folder, validity, uid)).fetchone():
                    continue
                status, parts = client.uid('FETCH', str(uid), '(BODY.PEEK[] INTERNALDATE)')
                tuples = [p for p in parts or [] if isinstance(p, tuple)]
                if status != 'OK' or not tuples:
                    raise RuntimeError('邮件读取失败，下次将从失败位置重试')
                header, raw = tuples[0]
                match = re.search(rb'INTERNALDATE "([^"]+)"', header)
                if not match:
                    raise RuntimeError('邮件缺少服务器收件时间')
                received = datetime.strptime(match[1].decode(), '%d-%b-%Y %H:%M:%S %z').astimezone(TZ).isoformat()
                identity, payload, candidate = parse_message(raw, received)
                with db:
                    if candidate:
                        result = db.execute('INSERT OR IGNORE INTO messages(id,received,payload) VALUES (?,?,?)',
                                            (identity, received, json.dumps(payload, ensure_ascii=False)))
                        count += result.rowcount
                    db.execute('INSERT OR IGNORE INTO seen VALUES (?,?,?)', (folder, validity, uid))
                    set_meta(db, cursor_key, dict(validity=validity, uid=uid))
                scanned += 1
        with db:
            set_meta(db, 'last_fetch_success', now())
        return dict(candidates=count, scanned=scanned, more=False)
    finally:
        try:
            client.logout()
        except Exception:
            pass
