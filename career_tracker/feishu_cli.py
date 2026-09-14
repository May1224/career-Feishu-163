"""Feishu CLI adapter: the CLI owns tokens; this process never reads them."""
import json
import os
import shutil
import subprocess
import re
from pathlib import Path
from uuid import uuid4

from .core import ROOT, connect, get_meta, save_json, set_meta
from .feishu import OVERVIEW, HISTORY, TRACKING_STAGES, FeishuError, plain


def invoke(command, **flags):
    node = shutil.which('node')
    shim = shutil.which('lark-cli.cmd')
    if not node or not shim:
        raise FeishuError('找不到飞书 CLI，请检查 Node.js 和全局安装')
    script = Path(shim).parent / 'node_modules/@larksuite/cli/scripts/run.js'
    argv = [node, str(script), 'base', command, '--as', 'user']
    if flags.get('format') != 'ndjson':
        argv += ['--format', 'json']
    for key, value in flags.items():
        name = '--' + key.replace('_', '-')
        if isinstance(value, bool):
            if value:
                argv.append(name)
        elif isinstance(value, list) and key == 'field_id':
            for item in value:
                argv += [name, item]
        else:
            argv += [name, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)]
    env = dict(os.environ, LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1', LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1')
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, encoding='utf-8', env=env, timeout=120)
    if proc.returncode:
        try:
            error = json.loads(proc.stderr).get('error', {})
            detail = str(error.get('subtype', error.get('type', 'unknown')))
        except (ValueError, AttributeError):
            detail = 'unknown'
        message = re.sub(r'https?://\S+', '[endpoint]', str(error.get('message', '')))[:250] if 'error' in locals() else ''
        failure = FeishuError(f'飞书 CLI {command} 失败（{detail}，退出码 {proc.returncode}）：{message}')
        failure.definite = detail in ('validation', 'missing_scope', 'not_configured', 'missing_token')
        raise failure
    result = json.loads(proc.stdout)
    if result.get('ok') is not True and not (flags.get('format') == 'ndjson' and result.get('format') == 'ndjson'):
        raise FeishuError('飞书 CLI 未返回成功标志')
    return result


def field_schema(fields, overview=None):
    names = list(fields)
    primary = '公司名' if '公司名' in names else ('事件' if '事件' in names else names[0])
    names.remove(primary)
    result = []
    for name in [primary] + names:
        kind = fields[name]
        field = {'name': name, 'type': {1: 'text', 3: 'select', 5: 'datetime', 7: 'checkbox', 18: 'link'}[kind]}
        if kind == 3:
            field.update(multiple=False, options=[{'name': stage} for stage in TRACKING_STAGES])
        if kind == 18:
            field.update(link_table=overview, bidirectional=False)
        if kind == 5:
            field['style'] = {'format': 'yyyy/MM/dd HH:mm'}
        result.append(field)
    return result


class FeishuCLI:
    def __init__(self, config):
        self.config = config

    def records(self, table):
        schema = OVERVIEW if table == self.config['overview_table'] else HISTORY
        rows, offset = [], 0
        temporary_files = []
        try:
            while True:
                output = f'data/.cli-records-{uuid4().hex}.ndjson'
                output_path = ROOT / output
                temporary_files.extend((output_path, output_path.with_suffix('.manifest.json')))
                response = invoke('+record-list', base_token=self.config['feishu_app_token'], table_id=table,
                                  field_id=list(schema), format='ndjson', output=output, overwrite=True,
                                  limit=2000, offset=offset)
                chunk = [json.loads(line) for line in output_path.read_text(encoding='utf-8').splitlines() if line.strip()]
                for row in chunk:
                    identifier = row.get('record_id') or row.get('id') or row.get('_record_id')
                    if not identifier:
                        raise FeishuError('飞书导出记录缺少 record_id')
                    rows.append({'record_id': identifier, 'fields': row.get('fields', row)})
                data = response.get('data', response)
                more = data.get('has_more', response.get('meta', {}).get('has_more'))
                if more is None:
                    raise FeishuError('飞书导出缺少完整性标记，停止同步')
                if not more:
                    return rows
                if not chunk:
                    raise FeishuError('飞书分页返回空结果')
                offset += len(chunk)
        finally:
            for path in temporary_files:
                path.unlink(missing_ok=True)

    def upsert(self, table, existing, fields, stable_id):
        fields = dict(fields)
        if '关联应聘' in fields:
            fields['关联应聘'] = [{'id': record_id} for record_id in fields['关联应聘']]
        # A create with an uncertain network result must reconcile before it can be retried.
        db = connect()
        key = 'cli-create:' + table + ':' + stable_id
        try:
            if not existing and get_meta(db, key):
                raise FeishuError('上次新增结果不确定且远端尚未查到记录；停止以避免重复，请稍后重试或人工核对')
            if not existing:
                with db:
                    set_meta(db, key, True)
            flags = dict(base_token=self.config['feishu_app_token'], table_id=table, json=fields)
            if existing:
                flags['record_id'] = existing['record_id']
            try:
                result = invoke('+record-upsert', **flags)
            except FeishuError as error:
                if getattr(error, 'definite', False):
                    with db:
                        set_meta(db, key, False)
                    raise
                # The write can reach Feishu even when its response is lost. Re-read by
                # our immutable key before retrying, so a later run cannot duplicate it.
                try:
                    matches = [row for row in self.records(table)
                               if plain(row['fields'].get('同步键')) == stable_id]
                except FeishuError:
                    raise error
                if len(matches) == 1:
                    with db:
                        set_meta(db, key, False)
                    return matches[0]['record_id']
                if len(matches) > 1:
                    raise FeishuError('飞书出现重复同步键，停止更新以免误写')
                raise error
            data = result.get('data', {})
            record = data.get('record', {})
            record_ids = record.get('record_id_list') or []
            record_id = (data.get('record_id') or record.get('record_id') or record.get('id')
                         or (record_ids[0] if len(record_ids) == 1 else None)
                         or (existing or {}).get('record_id'))
            if not record_id:
                matches = [row for row in self.records(table)
                           if plain(row['fields'].get('同步键')) == stable_id]
                if len(matches) == 1:
                    record_id = matches[0]['record_id']
                elif len(matches) > 1:
                    raise FeishuError('飞书出现重复同步键，停止更新以免误写')
                else:
                    raise FeishuError('新增返回缺少 record_id，且远端未查到同步键；请稍后重试')
            with db:
                set_meta(db, key, False)
            return record_id
        finally:
            db.close()


def initialize_cli(config, path):
    if not config.get('feishu_app_token'):
        receipt = ROOT / 'data/cli-base-create.json'
        if receipt.exists():
            result = json.loads(receipt.read_text(encoding='utf-8'))
        else:
            result = invoke('+base-create', name='求职进度', time_zone='Asia/Shanghai',
                            table_name='应聘总览', fields=field_schema(OVERVIEW))
            save_json(receipt, result)
        data = result['data'].get('base', result['data'])
        config['feishu_app_token'] = data.get('base_token') or data.get('app_token')
        config['base_url'] = data.get('url')
        if not config['feishu_app_token']:
            raise FeishuError('建表返回已保存，请检查 cli-base-create.json 中的 Base 标识')
        save_json(path, config)
    token = config['feishu_app_token']
    tables = invoke('+table-list', base_token=token)['data']['tables']
    for key, name, schema in [('overview_table', '应聘总览', OVERVIEW), ('history_table', '流程历史', HISTORY)]:
        matches = [t for t in tables if t['name'] == name]
        if not config.get(key):
            if len(matches) > 1:
                raise FeishuError('存在重名表，停止初始化')
            if matches:
                config[key] = matches[0]['id']
            else:
                result = invoke('+table-create', base_token=token, name=name,
                                fields=field_schema(schema, config['overview_table']))
                save_json(ROOT / ('data/cli-' + key + '-create.json'), result)
                data = result['data']
                config[key] = data.get('table', data).get('id') or data.get('table_id')
            if not config[key]:
                raise FeishuError('新建表返回已保存，但缺少表 ID')
            save_json(path, config)
        response = invoke('+field-list', base_token=token, table_id=config[key])
        existing = {field['name'] for field in response['data']['fields']}
        missing = [name for name in schema if name not in existing]
        if missing:
            additions = [field for field in field_schema({name: schema[name] for name in missing},
                                                        config['overview_table'])]
            response = invoke('+field-create', base_token=token, table_id=config[key], json=additions)
            save_json(ROOT / ('data/cli-' + key + '-field-additions.json'), response)
            response = invoke('+field-list', base_token=token, table_id=config[key])
        save_json(ROOT / ('data/cli-' + key + '-fields.json'), response)
    if not config.get('views_initialized'):
        table = config['overview_table']
        response = invoke('+view-list', base_token=token, table_id=table)
        save_json(ROOT / 'data/cli-views.json', response)
        views = response['data'].get('views', [])
        for name, flag in [('全部应聘', None), ('待处理', '待处理标记'), ('未来七天安排', '七天内安排'), ('待确认', '待确认标记')]:
            if not any(v['name'] == name for v in views):
                invoke('+view-create', base_token=token, table_id=table, json={'name': name, 'type': 'grid'})
            if flag:
                invoke('+view-set-filter', base_token=token, table_id=table, view_id=name,
                       json={'logic': 'and', 'conditions': [[flag, '==', '是']]})
        config['views_initialized'] = True
        save_json(path, config)
    return {'initialized': True}
