from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))
STAGES = ['邀请投递', '已投递', '待补充简历', '简历筛选', '待笔试', '笔试完成',
          '待面试', '面试完成', '待确认', 'Offer', '已拒绝', '已撤回']


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def connect(path=None):
    path = Path(path or ROOT / 'data/state.sqlite3')
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, received TEXT NOT NULL,
      payload TEXT NOT NULL, analyzed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS seen(folder TEXT, validity TEXT, uid INTEGER,
      PRIMARY KEY(folder,validity,uid));
    CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
      application_id TEXT NOT NULL, payload TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, ids TEXT NOT NULL,
      completed INTEGER NOT NULL DEFAULT 0, result_hash TEXT);
    CREATE TABLE IF NOT EXISTS mappings(kind TEXT, local_id TEXT, remote_id TEXT,
      PRIMARY KEY(kind,local_id));
    CREATE TABLE IF NOT EXISTS deliveries(kind TEXT, local_id TEXT, content_hash TEXT,
      PRIMARY KEY(kind,local_id));
    ''')
    return db


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))


def application_id(event, message_id):
    parts = [event[k].strip().casefold() for k in ('company', 'role', 'recruitment')]
    # Missing identity is isolated, never merged into another unknown application.
    return digest(parts if all(parts) else parts + [message_id])[:32]


def applications(db):
    result = {}
    records = [dict(json.loads(row['payload']), id=row['id'], application_id=row['application_id'],
                    message_id=row['message_id']) for row in db.execute('SELECT * FROM events')]
    for e in sorted(records, key=lambda e: (e['effective_at'], e['received'], e['message_id'], e.get('sequence', 0))):
        key = e['application_id']
        if key not in result:
            result[key] = dict(id=key, company=e['company'], role=e['role'], recruitment=e['recruitment'],
                               stage='待确认', next_action='', deadline=None, interview_at=None)
        app = result[key]
        # Absent updates preserve earlier facts; an explicit null clears obsolete dates.
        for field in ('stage', 'next_action', 'deadline', 'interview_at'):
            if field in e:
                app[field] = e[field]
        app.update(effective_at=e['effective_at'], evidence=e['evidence'], needs_review=e['needs_review'],
                   review_reason=e['review_reason'], subject=e['subject'], received=e['received'])
    return list(result.values())


def prepare(db, limit=20):
    active = db.execute('SELECT * FROM batches WHERE completed=0 ORDER BY rowid LIMIT 1').fetchone()
    ids = json.loads(active['ids']) if active else [r[0] for r in db.execute(
        'SELECT id FROM messages WHERE analyzed=0 ORDER BY received,id LIMIT ?', (limit,))]
    batch_id = active['id'] if active else digest(ids)[:32]
    if ids and not active:
        with db:
            db.execute('INSERT INTO batches(id,ids) VALUES (?,?)', (batch_id, json.dumps(ids)))
    messages = [dict(json.loads(db.execute('SELECT payload FROM messages WHERE id=?', (i,)).fetchone()[0]),
                     message_id=i) for i in ids]
    return dict(batch_id=batch_id, messages=messages, applications=applications(db), stages=STAGES)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('时间必须是包含时区的 ISO 8601 字符串')
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError('时间缺少时区')
    return dt.astimezone(TZ).isoformat(timespec='seconds')


def ingest(db, result):
    if not isinstance(result, dict) or set(result) != {'batch_id', 'results'}:
        raise ValueError('结果必须包含且仅包含 batch_id/results')
    batch = db.execute('SELECT * FROM batches WHERE id=?', (result['batch_id'],)).fetchone()
    if not batch:
        raise ValueError('未知批次')
    signature = digest(result)
    if batch['completed']:
        if batch['result_hash'] != signature:
            raise ValueError('已提交批次不可改写；请通过人工修正处理')
        return 0
    rows = result['results']
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError('results 必须为对象数组')
    ids = [r.get('message_id') for r in rows]
    if len(ids) != len(set(ids)) or set(ids) != set(json.loads(batch['ids'])):
        raise ValueError('结果必须完整覆盖当前批次且不可重复')
    validated = []
    for row in rows:
        if set(row) != {'message_id', 'relevant', 'reason', 'events'}:
            raise ValueError('邮件结果字段不符合约定')
        if type(row['relevant']) is not bool or not isinstance(row['reason'], str):
            raise ValueError('relevant/reason 类型错误')
        events = row['events']
        if not isinstance(events, list) or row['relevant'] != bool(events):
            raise ValueError('相关邮件必须至少有一个事件；无关邮件不得有事件')
        mail = json.loads(db.execute('SELECT payload FROM messages WHERE id=?', (row['message_id'],)).fetchone()[0])
        for index, original in enumerate(events):
            required = {'company', 'role', 'recruitment', 'event', 'effective_at', 'evidence', 'needs_review', 'review_reason'}
            optional = {'stage', 'next_action', 'deadline', 'interview_at', 'application_ref'}
            if not isinstance(original, dict) or not required <= original.keys() or original.keys() - required - optional:
                raise ValueError('事件字段不符合约定')
            e = original.copy()
            for key in required - {'needs_review'}:
                if not isinstance(e[key], str) or len(e[key]) > 2000:
                    raise ValueError('事件文本类型或长度错误')
            if not e['event'].strip() or not e['evidence'].strip():
                raise ValueError('事件和依据不可为空')
            if type(e['needs_review']) is not bool:
                raise ValueError('needs_review 必须是布尔值')
            if e.get('stage') is not None and e['stage'] not in STAGES:
                raise ValueError('未知阶段')
            if 'stage' in e and e['stage'] is None:
                raise ValueError('stage 不可为 null')
            if 'next_action' in e and (not isinstance(e['next_action'], str) or len(e['next_action']) > 2000):
                raise ValueError('next_action 必须为文本')
            e['effective_at'] = timestamp(e['effective_at'])
            for key in ('deadline', 'interview_at'):
                if key in e and e[key] is not None:
                    e[key] = timestamp(e[key])
            if not all(e[k].strip() for k in ('company', 'role', 'recruitment')) or mail.get('truncated'):
                e['needs_review'] = True
                e['review_reason'] = e['review_reason'] or '身份信息缺失或正文截断'
            if e['needs_review'] and not e['review_reason'].strip():
                raise ValueError('待确认事件必须说明原因')
            e.update(subject=mail['subject'], received=mail['received'], sequence=index)
            target = application_id(e, row['message_id'])
            if 'application_ref' in e:
                ref = e.pop('application_ref')
                known = next((json.loads(v[3]) for v in validated if v[2] == ref), None)
                if known is None:
                    record = db.execute('SELECT payload FROM events WHERE application_id=? LIMIT 1', (ref,)).fetchone()
                    known = json.loads(record[0]) if record else None
                if not known or not e['company'].strip() or any(known[k].strip().casefold() != e[k].strip().casefold()
                        for k in ('company', 'role', 'recruitment')):
                    raise ValueError('application_ref 未知或与公司、岗位、批次不符')
                target = ref
            validated.append((digest([row['message_id'], index]), row['message_id'], target, json.dumps(e, ensure_ascii=False)))
    with db:
        db.executemany('INSERT INTO events VALUES (?,?,?,?)', validated)
        db.executemany('UPDATE messages SET analyzed=1 WHERE id=?', [(i,) for i in ids])
        db.execute('UPDATE batches SET completed=1,result_hash=? WHERE id=?', (signature, result['batch_id']))
    return len(validated)
