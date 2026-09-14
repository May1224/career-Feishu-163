import argparse
import json
import os
import sys
from contextlib import contextmanager

from . import credentials
from .core import ROOT, applications, connect, get_meta, ingest, now, prepare, save_json, set_meta
from .feishu import Feishu, initialize, sync
from .mailbox import fetch


def config():
    path = ROOT / 'config.json'
    if not path.exists():
        raise RuntimeError('尚未配置，请双击 配置邮箱和飞书.cmd')
    value = json.loads(path.read_text(encoding='utf-8'))
    required = ('email',) if value.get('backend') == 'cli' else ('email', 'feishu_app_id', 'feishu_app_token')
    if not all(value.get(k) for k in required):
        raise RuntimeError('配置不完整，请运行配置窗口')
    return value


@contextmanager
def lock():
    # OS file lock releases on process exit, including crashes; no stale PID removal.
    import msvcrt
    path = ROOT / 'data/run.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('另一个命令正在执行，请稍后重试') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run_cycle(db, settings, password, api, max_new, fetcher=fetch):
    """Fetch one bounded batch, then sync only when every candidate is analyzed."""
    fetched = fetcher(db, settings, password, max_new)
    pending = db.execute('SELECT count(*) FROM messages WHERE analyzed=0').fetchone()[0]
    if pending:
        return {'fetch': fetched, 'pending_messages': pending,
                'next': '运行 prepare，完成 Codex 分析和 ingest 后再运行 run'}
    if not settings.get('overview_table') or not settings.get('history_table'):
        raise RuntimeError('请先运行 init-feishu')
    return {'fetch': fetched, 'sync': sync(db, api, settings)}


def main():
    parser = argparse.ArgumentParser(description='163 招聘邮件 / Codex / 飞书多维表格')
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('status', 'doctor', 'init-feishu', 'sync', 'mark-ready'):
        subs.add_parser(name)
    collect = subs.add_parser('fetch')
    collect.add_argument('--max-new', type=int, default=200)
    run = subs.add_parser('run', help='读取一批新邮件；无待分析邮件时同步飞书')
    run.add_argument('--max-new', type=int, default=200)
    batch = subs.add_parser('prepare')
    batch.add_argument('--limit', type=int, default=20)
    result = subs.add_parser('ingest')
    result.add_argument('file')
    args = parser.parse_args()
    db = connect()
    try:
        with lock():
            if args.command == 'status':
                output = {'configured': (ROOT / 'config.json').exists(),
                          'ready': get_meta(db, 'ready', False),
                          'pending_messages': db.execute('SELECT count(*) FROM messages WHERE analyzed=0').fetchone()[0],
                          'applications': len(applications(db)),
                          'last_fetch_success': get_meta(db, 'last_fetch_success'),
                          'last_sync_success': get_meta(db, 'last_sync_success'),
                          'last_error': get_meta(db, 'last_error')}
            elif args.command == 'prepare':
                if not 1 <= args.limit <= 50:
                    raise ValueError('批次大小须在 1–50 之间')
                output = prepare(db, args.limit)
                save_json(ROOT / 'data/pending.json', output)
                output = {'batch_id': output['batch_id'], 'messages': len(output['messages']),
                          'path': str(ROOT / 'data/pending.json')}
            elif args.command == 'ingest':
                output = {'accepted_events': ingest(db, json.loads(open(args.file, encoding='utf-8-sig').read()))}
            elif args.command == 'mark-ready':
                if not get_meta(db, 'last_fetch_success') or not get_meta(db, 'last_sync_success') or not get_meta(db, 'schema_verified'):
                    raise RuntimeError('需要先完成邮箱读取、飞书建表和同步验证')
                if db.execute('SELECT count(*) FROM messages WHERE analyzed=0').fetchone()[0]:
                    raise RuntimeError('历史候选邮件尚未全部分析')
                with db:
                    set_meta(db, 'ready', True)
                output = {'ready': True, 'next': '让 Codex 启用已暂停的“求职邮件跟踪”定时任务'}
            else:
                settings = config()
                if args.command in ('fetch', 'run'):
                    if not 1 <= args.max_new <= 2000:
                        raise ValueError('max-new 须在 1–2000 之间')
                    if args.command == 'fetch':
                        output = fetch(db, settings, credentials.get('imap_authorization'), args.max_new)
                    else:
                        from .feishu_cli import FeishuCLI
                        cli = settings.get('backend') == 'cli'
                        api = FeishuCLI(settings) if cli else Feishu(settings, credentials.get('feishu_secret'))
                        output = run_cycle(db, settings, credentials.get('imap_authorization'), api, args.max_new)
                else:
                    from .feishu_cli import FeishuCLI, initialize_cli
                    cli = settings.get('backend') == 'cli'
                    api = FeishuCLI(settings) if cli else Feishu(settings, credentials.get('feishu_secret'))
                    if args.command == 'init-feishu':
                        if cli:
                            initialize_cli(settings, ROOT / 'config.json')
                        else:
                            initialize(api, settings, ROOT / 'config.json')
                        with db:
                            set_meta(db, 'schema_verified', now())
                        output = {'schema_verified': True}
                    elif args.command == 'doctor':
                        credentials.get('imap_authorization')
                        if cli:
                            from .feishu_cli import invoke
                            invoke('+table-list', base_token=settings['feishu_app_token'])
                        else:
                            api.listing(api.base + '/tables')
                        output = {'credentials_available': True, 'feishu_readable': True,
                                  'next': '运行 fetch --max-new 5 验证邮箱只读访问，再运行 init-feishu 验证写入'}
                    else:
                        if not settings.get('overview_table') or not settings.get('history_table'):
                            raise RuntimeError('请先运行 init-feishu')
                        output = sync(db, api, settings)
            if args.command not in ('status', 'prepare'):
                with db:
                    set_meta(db, 'last_error', None)
            save_json(ROOT / 'data/last-command.json', {'command': args.command, 'at': now(), 'result': output})
            print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        # Only our explicit diagnostic messages are safe to persist; IMAP/HTTP errors can echo secrets/body.
        if type(error) in (RuntimeError, ValueError) or error.__class__.__name__ == 'FeishuError':
            message = str(error) if type(error) is not ValueError else '输入数据校验失败，请检查结构、时间和完整批次'
        else:
            message = '执行失败（' + type(error).__name__ + '）；检查网络和配置，未输出原始错误内容'
        with db:
            set_meta(db, 'last_error', {'at': now(), 'command': args.command, 'message': message})
        print(json.dumps({'error': message}, ensure_ascii=False))
        return 1
    finally:
        db.close()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
