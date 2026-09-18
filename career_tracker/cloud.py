"""Helpers for GitHub-hosted, stateless runners."""
from __future__ import annotations

import json
from datetime import datetime

from .core import TZ, now, set_meta
from .feishu import plain


def _text(fields, name):
    return plain(fields.get(name)).strip()


def _date(fields, name, fallback):
    value = fields.get(name)
    if isinstance(value, list) and value:
        value = value[0]
    try:
        return datetime.fromtimestamp(int(value) / 1000, TZ).isoformat(timespec='seconds')
    except (TypeError, ValueError, OSError):
        return fallback


def _truth(value):
    return value is True or plain(value) in ('是', 'true', 'True', '1')


def restore(db, api, config):
    """Restore IMAP cursors and application snapshots from Feishu."""
    for row in api.records(config['system_table']):
        key, value = _text(row['fields'], '同步键'), _text(row['fields'], '内容')
        if key.startswith('cursor:') or key in ('initial_since', 'cloud_initialized'):
            try:
                with db:
                    set_meta(db, key, json.loads(value))
            except json.JSONDecodeError:
                continue
    valid = {'邀请投递', '已投递', '待补充简历', '简历筛选', '待笔试', '笔试完成',
             '待面试', '面试完成', '待确认', 'Offer', '已拒绝', '已撤回'}
    for row in api.records(config['overview_table']):
        fields, app_id = row['fields'], _text(row['fields'], '同步键')
        if not app_id:
            continue
        received = _date(fields, '最近邮件时间', now())
        stage = _text(fields, '当前有效阶段') or _text(fields, '邮件识别阶段') or '待确认'
        payload = {'company': _text(fields, '公司名'), 'role': _text(fields, '岗位名'),
                   'recruitment': _text(fields, '招聘批次'), 'event': '恢复远端状态',
                   'effective_at': received, 'stage': stage if stage in valid else '待确认',
                   'next_action': _text(fields, '下一步待办'),
                   'deadline': _date(fields, '截止时间', None), 'interview_at': _date(fields, '面试时间', None),
                   'evidence': _text(fields, '依据摘要') or '飞书既有记录',
                   'needs_review': _truth(fields.get('待确认')), 'review_reason': _text(fields, '待确认原因'),
                   'subject': _text(fields, '邮件标题'), 'received': received, 'sequence': 0}
        with db:
            db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                       ('snapshot:' + app_id, 'snapshot:' + app_id, app_id, json.dumps(payload, ensure_ascii=False)))


def save_cursors(db, api, config):
    existing = {plain(row['fields'].get('同步键')): row for row in api.records(config['system_table'])}
    for (key,) in db.execute("SELECT key FROM meta WHERE key IN ('initial_since','cloud_initialized') OR key LIKE 'cursor:%'"):
        value = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()[0]
        api.upsert(config['system_table'], existing.get(key), {'同步键': key, '内容': value}, key)
