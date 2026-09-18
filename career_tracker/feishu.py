import json
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import TZ, applications, digest, now, save_json, set_meta


class FeishuError(RuntimeError):
    pass


class Feishu:
    def __init__(self, config, secret):
        self.config = config
        self.token = None
        response = self.call('POST', '/auth/v3/tenant_access_token/internal',
                             {'app_id': config['feishu_app_id'], 'app_secret': secret})
        self.token = response['tenant_access_token']
        self.base = '/bitable/v1/apps/' + config['feishu_app_token']

    def call(self, method, path, payload=None, query=None):
        url = 'https://open.feishu.cn/open-apis' + path
        if query:
            url += '?' + urlencode(query)
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        # Record creates carry a stable idempotency token. Schema creates are reconciled on the next init.
        retryable = method in ('GET', 'PUT', 'PATCH') or bool(query and query.get('client_token'))
        for attempt in range(4):
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
                    result = json.load(response)
                code = result.get('code', 0)
                if code:
                    if code in (99991400, 99991401, 1254290) and retryable and attempt < 3:
                        time.sleep(2 ** attempt)
                        continue
                    raise FeishuError(f'飞书接口失败，代码 {code}；检查应用权限和表格访问授权')
                return result
            except HTTPError as error:
                if retryable and (error.code == 429 or error.code >= 500) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                # Do not expose Feishu's body or Base token.  The redacted route
                # is enough to distinguish token creation, table setup and writes.
                route = re.sub(r'(/apps/)[^/]+', r'\1[app-token]', path)
                try:
                    service_error = json.loads(error.read().decode('utf-8', 'replace'))
                    service_code = service_error.get('code')
                    violations = (service_error.get('error') or {}).get('permission_violations', [])
                    scopes = sorted({item.get('subject') for item in violations
                                     if isinstance(item, dict) and isinstance(item.get('subject'), str)})
                except (ValueError, UnicodeDecodeError, AttributeError):
                    service_code = None
                    scopes = []
                code_hint = f'，飞书代码 {service_code}' if isinstance(service_code, int) else ''
                scope_hint = '，缺少权限 ' + '、'.join(scopes) if scopes else ''
                raise FeishuError(f'飞书 HTTP {error.code}（{method} {route}{code_hint}{scope_hint}）；未输出响应正文以保护数据') from None
            except (URLError, TimeoutError, OSError):
                if retryable and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise FeishuError('飞书网络连接失败，保留本地数据供重试') from None

    def listing(self, path):
        items, token = [], None
        while True:
            query = {'page_size': 100}
            if token:
                query['page_token'] = token
            data = self.call('GET', path, query=query)['data']
            items.extend(data.get('items') or [])
            if not data.get('has_more'):
                return items
            token = data.get('page_token')
            if not token:
                raise FeishuError('分页缺少游标')

    def records(self, table):
        return self.listing(self.base + '/tables/' + table + '/records')

    def upsert(self, table, existing, fields, stable_id):
        path = self.base + '/tables/' + table + '/records'
        if existing:
            self.call('PUT', path + '/' + existing['record_id'], {'fields': fields})
            return existing['record_id']
        # Bitable's client_token accepts UUID v4.  Keep one generated token for
        # all network retries in this invocation; later runs reconcile by 同步键.
        token = str(uuid.uuid4())
        result = self.call('POST', path, {'fields': fields}, {'client_token': token})
        return result['data']['record']['record_id']


TRACKING_STAGES = ('已投递', '测评中', '笔试中', '面试中', '已淘汰')


OVERVIEW = {'同步键': 1, '公司名': 1, '岗位名': 1, '招聘批次': 1, '邮件识别阶段': 1,
            '人工修正阶段': 1, '当前有效阶段': 1, '下一步待办': 1, '截止时间': 5,
            '面试时间': 5, '最近邮件时间': 5, '邮件标题': 1, '依据摘要': 1,
            '备注': 1, '待确认': 7, '待确认原因': 1, '待处理标记': 1, '七天内安排': 1,
            '待确认标记': 1, '最近同步时间': 5, '流程阶段': 3,
            '阶段截止时间': 5, '收件日期': 5}
HISTORY = {'同步键': 1, '应聘同步键': 1, '事件': 1, '事件时间': 5, '邮件时间': 5,
           '邮件标识': 1, '邮件标题': 1, '依据摘要': 1, '待确认': 7, '关联应聘': 18}
SYSTEM = {'同步键': 1, '内容': 1}


def initialize(api, config, path=None):
    tables = api.listing(api.base + '/tables')
    for key, title, schema in [('overview_table', '应聘总览', OVERVIEW),
                               ('history_table', '流程历史', HISTORY),
                               ('system_table', '系统状态', SYSTEM)]:
        matches = [t for t in tables if t['name'] == title]
        if not config.get(key):
            if len(matches) > 1:
                raise FeishuError('存在重名数据表，请在配置中指定表 ID')
            if matches:
                config[key] = matches[0]['table_id']
            else:
                config[key] = api.call('POST', api.base + '/tables', {'table': {'name': title}})['data']['table_id']
            if path:
                save_json(path, config)
        table = config[key]
        base = api.base + '/tables/' + table
        fields = {f['field_name']: f for f in api.listing(base + '/fields')}
        for name, kind in schema.items():
            if name in fields:
                if fields[name]['type'] != kind:
                    raise FeishuError('已有字段类型不匹配：' + name)
                continue
            payload = {'field_name': name, 'type': kind}
            if kind == 3:
                payload['property'] = {'options': [{'name': stage} for stage in TRACKING_STAGES]}
            if kind == 18:
                payload['property'] = {'table_id': config['overview_table'], 'multiple': False}
            fields[name] = api.call('POST', base + '/fields', payload)['data']['field']
        if key == 'overview_table':
            views = api.listing(base + '/views')
            for title, flag in [('全部应聘', None), ('待处理', '待处理标记'),
                                ('未来七天安排', '七天内安排'), ('待确认', '待确认标记')]:
                matches = [v for v in views if v['view_name'] == title]
                if len(matches) > 1:
                    raise FeishuError('存在重名视图，请先清理：' + title)
                view = matches[0] if matches else api.call('POST', base + '/views',
                    {'view_name': title, 'view_type': 'grid'})['data']['view']
                if flag:
                    api.call('PATCH', base + '/views/' + view['view_id'], {'property': {'filter_info': {
                        'conjunction': 'and', 'conditions': [{'field_id': fields[flag]['field_id'],
                            'operator': 'is', 'value': json.dumps(['是'], ensure_ascii=False)}]}}})
    return config


def plain(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return ''.join(str(v.get('text', '')) if isinstance(v, dict) else str(v) for v in value)
    return str(value)


def index_records(records):
    result = {}
    for row in records:
        key = plain(row['fields'].get('同步键'))
        if not key:
            continue
        if key in result:
            raise FeishuError('飞书存在重复同步键，停止更新以免误写')
        result[key] = row
    return result


def millis(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000) if value else None


def tracking_stage(stage, source=''):
    """Map detailed evidence to the five statuses shown in the user-facing view."""
    if stage in TRACKING_STAGES:
        return stage
    if stage in ('已拒绝', '已撤回'):
        return '已淘汰'
    if stage in ('待笔试', '笔试完成'):
        return '测评中' if '测评' in source else '笔试中'
    if stage in ('待面试', '面试完成', 'Offer'):
        return '面试中'
    return '已投递'


def overview_fields(app, remote, current=None):
    current = current or datetime.now(TZ)
    manual = plain(remote.get('人工修正阶段')).strip()
    stage = manual or app['stage']
    conflict = bool(manual and manual != app['stage'])
    review = app['needs_review'] or conflict
    visible_stage = tracking_stage(stage, app['subject'] + '\n' + app['evidence'])
    visible_deadline = app['interview_at'] if visible_stage == '面试中' and app['interview_at'] else app['deadline']
    upcoming = any(current <= datetime.fromisoformat(value) <= current + timedelta(days=7)
                   for value in (app.get('deadline'), app.get('interview_at')) if value)
    return {'同步键': app['id'], '公司名': app['company'] or '待确认', '岗位名': app['role'] or '待确认',
            '招聘批次': app['recruitment'] or '待确认', '邮件识别阶段': app['stage'], '当前有效阶段': stage,
            '下一步待办': app['next_action'], '截止时间': millis(app['deadline']),
            '面试时间': millis(app['interview_at']), '最近邮件时间': millis(app['received']),
            '邮件标题': app['subject'], '依据摘要': app['evidence'], '待确认': review,
            '待确认原因': '；'.join(x for x in (app['review_reason'], '人工修正与邮件阶段不同' if conflict else '') if x),
            '待处理标记': '是' if stage in ('邀请投递', '待补充简历', '待笔试', '待面试', '待确认', 'Offer') or review else '否',
            '七天内安排': '是' if upcoming else '否', '待确认标记': '是' if review else '否',
            '流程阶段': visible_stage, '阶段截止时间': millis(visible_deadline),
            '收件日期': millis(app['received'])}


def sync(db, api, config):
    overview = index_records(api.records(config['overview_table']))
    history = index_records(api.records(config['history_table']))
    changed = 0
    links = {}
    active = applications(db)
    active_ids = {app['id'] for app in active}
    changed_apps = {row['application_id'] for row in db.execute(
        "SELECT application_id FROM events WHERE id NOT LIKE 'snapshot:%'")}

    def deliver(kind, key, table, remote, fields):
        nonlocal changed
        signature = digest(fields)
        previous = db.execute('SELECT content_hash FROM deliveries WHERE kind=? AND local_id=?', (kind, key)).fetchone()
        existing = remote.get(key)
        if existing and previous and previous[0] == signature:
            record_id = existing['record_id']
        else:
            outgoing = dict(fields)
            if kind == 'overview':
                outgoing['最近同步时间'] = millis(now())
            record_id = api.upsert(table, existing, outgoing, key)
            changed += 1
        with db:
            db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?,?)', (kind, key, record_id))
            db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?)', (kind, key, signature))
        return record_id

    for app in active:
        key = app['id']
        remote = overview.get(key, {})
        # A cloud runner restores this app from Feishu on every invocation.  Do
        # not rewrite unchanged snapshots merely because its local SQLite file
        # is new; applications with a fresh mail event are still updated.
        if remote and key not in changed_apps:
            links[key] = remote['record_id']
        else:
            fields = overview_fields(app, remote.get('fields', {}))
            links[key] = deliver('overview', key, config['overview_table'], overview, fields)
    for row in db.execute("SELECT * FROM events WHERE id NOT LIKE 'snapshot:%' ORDER BY id").fetchall():
        if row['application_id'] not in active_ids:
            continue
        e = json.loads(row['payload'])
        fields = {'同步键': row['id'], '应聘同步键': row['application_id'], '事件': e['event'],
                  '事件时间': millis(e['effective_at']), '邮件时间': millis(e['received']),
                  '邮件标识': row['message_id'], '邮件标题': e['subject'], '依据摘要': e['evidence'],
                  '待确认': e['needs_review'], '关联应聘': [links[row['application_id']]]}
        deliver('history', row['id'], config['history_table'], history, fields)
    with db:
        set_meta(db, 'last_sync_success', now())
    return {'changed_records': changed, 'applications': len(links)}
