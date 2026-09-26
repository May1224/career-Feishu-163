import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from career_tracker.core import connect, get_meta, ingest, prepare, applications, suppress_before, TZ
from career_tracker.feishu import overview_fields, sync, index_records, FeishuError
from career_tracker.mailbox import parse_message, fetch, folder_names
from career_tracker.model import SCHEMA, analyze as analyze_with_model
from career_tracker.__main__ import run_cycle


def event(**kwargs):
    return dict(company='示例公司', role='测试开发', recruitment='2027届秋招', event='收到面试邀请',
                effective_at='2026-09-14T10:00:00+08:00', stage='待面试', next_action='参加面试',
                interview_at='2026-09-16T15:00:00+08:00', evidence='邀请16日15点参加面试',
                needs_review=False, review_reason='', **kwargs)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = connect(Path(self.temp.name) / 'state.db')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def add_mail(self, identity, subject='面试邀请'):
        payload = {'subject': subject, 'received': '2026-09-14T10:00:00+08:00', 'body': '邀请参加面试'}
        with self.db:
            self.db.execute('INSERT INTO messages(id,received,payload) VALUES (?,?,?)',
                            (identity, payload['received'], json.dumps(payload)))

    def result(self, events):
        batch = prepare(self.db)
        return {'batch_id': batch['batch_id'], 'results': [
            {'message_id': mail['message_id'], 'relevant': bool(events[mail['message_id']]),
             'reason': '测试', 'events': events[mail['message_id']]} for mail in batch['messages']]}


class StoreTests(StoreFixture):
    def test_same_time_events_preserve_declared_sequence(self):
        self.add_mail('m1')
        first = dict(event(), stage='已投递')
        ingest(self.db, self.result({'m1': [first, event()]}))
        self.assertEqual(applications(self.db)[0]['stage'], '待面试')

    def test_repeated_batch_and_ingest_are_idempotent(self):
        self.add_mail('m1')
        self.assertEqual(prepare(self.db)['batch_id'], prepare(self.db)['batch_id'])
        result = self.result({'m1': [event()]})
        self.assertEqual(ingest(self.db, result), 1)
        self.assertEqual(ingest(self.db, result), 0)
        self.assertEqual(len(applications(self.db)), 1)
        self.assertEqual(prepare(self.db)['messages'], [])

    def test_two_roles_and_unknown_identity_do_not_merge(self):
        for i in ('m1', 'm2', 'm3', 'm4'):
            self.add_mail(i)
        other = dict(event(), role='后端开发')
        unknown = dict(event(), recruitment='')
        ingest(self.db, self.result({'m1': [event()], 'm2': [other], 'm3': [unknown], 'm4': [unknown]}))
        apps = applications(self.db)
        self.assertEqual(len(apps), 4)
        self.assertTrue(all(a['needs_review'] for a in apps if not a['recruitment']))

    def test_reschedule_and_old_mail_does_not_regress(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        self.add_mail('m2')
        moved = dict(event(), effective_at='2026-09-15T10:00:00+08:00', interview_at='2026-09-18T16:00:00+08:00')
        ingest(self.db, self.result({'m2': [moved]}))
        self.add_mail('m3')
        older = dict(event(), effective_at='2026-09-10T10:00:00+08:00', stage='已投递')
        ingest(self.db, self.result({'m3': [older]}))
        app = applications(self.db)[0]
        self.assertEqual(app['stage'], '待面试')
        self.assertEqual(app['interview_at'], '2026-09-18T16:00:00+08:00')

    def test_explicit_null_clears_and_omission_preserves(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        self.add_mail('m2')
        update = dict(event(), effective_at='2026-09-15T10:00:00+08:00')
        update.pop('interview_at')
        ingest(self.db, self.result({'m2': [update]}))
        self.assertIsNotNone(applications(self.db)[0]['interview_at'])
        self.add_mail('m3')
        update = dict(update, effective_at='2026-09-16T10:00:00+08:00', stage='已拒绝', interview_at=None)
        ingest(self.db, self.result({'m3': [update]}))
        self.assertIsNone(applications(self.db)[0]['interview_at'])

    def test_validation_is_atomic(self):
        self.add_mail('m1')
        self.add_mail('m2')
        invalid = dict(event(), stage='不支持的流程阶段')
        with self.assertRaises(ValueError):
            ingest(self.db, self.result({'m1': [event()], 'm2': [invalid]}))
        self.assertEqual(self.db.execute('SELECT count(*) FROM events').fetchone()[0], 0)
        self.assertEqual(len(prepare(self.db)['messages']), 2)

    def test_invalid_model_time_is_retained_for_review(self):
        self.add_mail('m1')
        invalid = dict(event(), effective_at='2026年9月14日', deadline='下周三', interview_at='下午三点')
        ingest(self.db, self.result({'m1': [invalid]}))
        app = applications(self.db)[0]
        self.assertEqual(app['effective_at'], '2026-09-14T10:00:00+08:00')
        self.assertTrue(app['needs_review'])
        self.assertIn('时间格式无效', app['review_reason'])
        self.assertIsNone(app['deadline'])
        self.assertIsNone(app['interview_at'])

    def test_reject_missing_and_duplicate_results(self):
        self.add_mail('m1')
        result = self.result({'m1': [event()]})
        result['results'] *= 2
        with self.assertRaises(ValueError):
            ingest(self.db, result)

    def test_manual_override_and_notes_are_not_written(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        fields = overview_fields(applications(self.db)[0], {'人工修正阶段': [{'text': '面试完成'}], '备注': '已参加'},
                                 datetime.fromisoformat('2026-09-14T20:00:00+08:00'))
        self.assertEqual(fields['当前有效阶段'], '面试完成')
        self.assertTrue(fields['待确认'])
        self.assertNotIn('人工修正阶段', fields)
        self.assertNotIn('备注', fields)
        self.assertEqual(fields['七天内安排'], '是')

    def test_user_facing_stage_and_interview_time_are_simplified(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        fields = overview_fields(applications(self.db)[0], {})
        self.assertEqual(fields['公司名'], '示例公司')
        self.assertEqual(fields['岗位名'], '测试开发')
        self.assertEqual(fields['流程阶段'], '面试中')
        self.assertEqual(fields['阶段截止时间'], fields['面试时间'])

    def test_sync_retry_after_remote_commit_and_local_failure(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        api = FakeFeishu()
        settings = {'overview_table': 'overview', 'history_table': 'history'}
        api.fail_after_create = True
        with self.assertRaises(FeishuError):
            sync(self.db, api, settings)
        self.assertIsNone(get_meta(self.db, 'last_sync_success'))
        sync(self.db, api, settings)
        self.assertEqual(len(api.tables['overview']), 1)
        self.assertEqual(len(api.tables['history']), 1)
        self.assertEqual(sync(self.db, api, settings)['changed_records'], 0)

    def test_irrelevant_candidate_consumed_without_application(self):
        self.add_mail('m1', '招聘广告')
        ingest(self.db, self.result({'m1': []}))
        self.assertEqual(applications(self.db), [])
        self.assertEqual(prepare(self.db)['messages'], [])

    def test_suppression_hides_old_application_and_its_history_from_sync(self):
        self.add_mail('m1')
        ingest(self.db, self.result({'m1': [event()]}))
        hidden = suppress_before(self.db, datetime.fromisoformat('2026-09-15T00:00:00+08:00'))
        self.assertEqual(len(hidden), 1)
        self.assertEqual(applications(self.db), [])
        api = FakeFeishu()
        result = sync(self.db, api, {'overview_table': 'overview', 'history_table': 'history'})
        self.assertEqual(result['applications'], 0)
        self.assertEqual(api.tables['history'], [])

    def test_run_cycle_waits_for_analysis_before_syncing(self):
        self.add_mail('m1')
        result = run_cycle(self.db, {'overview_table': 'overview', 'history_table': 'history'}, 'unused',
                           FakeFeishu(), 10, fetcher=lambda *_: {'candidates': 1, 'scanned': 1, 'more': False})
        self.assertEqual(result['pending_messages'], 1)
        self.assertIn('prepare', result['next'])

    def test_run_cycle_syncs_when_no_analysis_is_pending(self):
        result = run_cycle(self.db, {'overview_table': 'overview', 'history_table': 'history'}, 'unused',
                           FakeFeishu(), 10, fetcher=lambda *_: {'candidates': 0, 'scanned': 0, 'more': False})
        self.assertEqual(result['sync']['applications'], 0)


class FakeFeishu:
    def __init__(self):
        self.tables = {'overview': [], 'history': []}
        self.fail_after_create = False

    def records(self, table):
        return copy.deepcopy(self.tables[table])

    def upsert(self, table, existing, fields, stable_id):
        if existing:
            row = next(r for r in self.tables[table] if r['record_id'] == existing['record_id'])
            row['fields'].update(fields)
        else:
            row = {'record_id': 'rec' + str(len(self.tables[table]) + 1), 'fields': dict(fields)}
            self.tables[table].append(row)
        if self.fail_after_create:
            self.fail_after_create = False
            raise FeishuError('模拟服务端已写入但客户端未收到响应')
        return row['record_id']


class ModelTests(unittest.TestCase):
    def test_model_request_includes_the_required_schema(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        result = {'choices': [{'message': {'content': '{"batch_id":"batch","results":[]}'}}]}
        with patch('career_tracker.model.urlopen', return_value=Response()) as request, \
                patch('career_tracker.model.json.load', return_value=result):
            self.assertEqual(analyze_with_model({'batch_id': 'batch', 'messages': [], 'stages': []}, 'test-key'),
                             {'batch_id': 'batch', 'results': []})
        body = __import__('json').loads(request.call_args.args[0].data)
        self.assertIn('"batch_id"', body['messages'][0]['content'])
        self.assertIn('"additionalProperties":false', body['messages'][0]['content'])
        self.assertEqual(SCHEMA['type'], 'object')

    def test_agnes_request_uses_documented_compatibility_subset(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        result = {'choices': [{'message': {'content': '{"batch_id":"batch","results":[]}'}}]}
        with patch.dict('career_tracker.model.os.environ', {'LLM_PROVIDER': 'agnes'}, clear=False), \
                patch('career_tracker.model.urlopen', return_value=Response()) as request, \
                patch('career_tracker.model.json.load', return_value=result):
            analyze_with_model({'batch_id': 'batch', 'messages': [], 'stages': []}, 'test-key')
        body = json.loads(request.call_args.args[0].data)
        self.assertNotIn('response_format', body)


RAW = ('From: hr@example.com\r\nSubject: interview invitation\r\n'
       'Date: Mon, 14 Sep 2026 10:00:00 +0800\r\nMessage-ID: <test@example.com>\r\n'
       'Content-Type: text/plain; charset=utf-8\r\n\r\nPlease attend interview.').encode()


class FakeIMAP:
    capabilities = ()
    commands = []
    fail_uid = None

    def __init__(self, *args, **kwargs):
        pass

    def login(self, *args):
        return 'OK', []

    def list(self):
        return 'OK', [b'(\\HasNoChildren) "/" "INBOX"', b'(\\Junk) "/" "Spam"']

    def select(self, folder, readonly=False):
        self.commands.append(('select', readonly))
        return 'OK', [b'2']

    def response(self, key):
        return key, [b'1']

    def uid(self, command, *args):
        self.commands.append((command, args))
        if command == 'SEARCH':
            return 'OK', [b'1 2']
        if int(args[0]) == self.fail_uid:
            return 'NO', []
        # Identical mail copy must not create another pending message.
        return 'OK', [(b'1 (UID 1 INTERNALDATE "14-Sep-2026 10:00:00 +0800")', RAW)]

    def logout(self):
        pass


class MailTests(StoreFixture):
    def test_readonly_and_duplicate_mail_and_catchup(self):
        FakeIMAP.commands = []
        FakeIMAP.fail_uid = None
        result = fetch(self.db, {'email': 'user@163.com'}, 'not-a-real-secret', 1, FakeIMAP)
        self.assertTrue(result['more'])
        self.assertEqual(get_meta(self.db, 'cursor:"INBOX"')['uid'], 1)
        fetch(self.db, {'email': 'user@163.com'}, 'not-a-real-secret', 10, FakeIMAP)
        self.assertEqual(self.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        self.assertTrue(all(args is True for command, args in FakeIMAP.commands if command == 'select'))
        self.assertTrue(all('BODY.PEEK[]' in args[1] for command, args in FakeIMAP.commands if command == 'FETCH'))

    def test_initial_scan_has_no_date_limit_by_default(self):
        FakeIMAP.commands = []
        fetch(self.db, {'email': 'user@163.com'}, 'not-a-real-secret', 10, FakeIMAP)
        searches = [args for command, args in FakeIMAP.commands if command == 'SEARCH']
        self.assertTrue(all(args[-1] == 'ALL' for args in searches))

    def test_failure_does_not_advance_cursor_past_failed_mail(self):
        FakeIMAP.fail_uid = 2
        try:
            with self.assertRaises(RuntimeError):
                fetch(self.db, {'email': 'user@163.com'}, 'not-a-real-secret', 10, FakeIMAP)
            self.assertEqual(get_meta(self.db, 'cursor:"INBOX"')['uid'], 1)
            self.assertIsNone(get_meta(self.db, 'last_fetch_success'))
        finally:
            FakeIMAP.fail_uid = None

    def test_html_removes_active_content(self):
        raw = RAW.replace(b'text/plain', b'text/html').replace(b'Please attend interview.',
                    b'<style>secret</style><p>interview</p><script>steal()</script>')
        _, payload, candidate = parse_message(raw, '2026-09-14T10:00:00+08:00')
        self.assertTrue(candidate)
        self.assertNotIn('steal', payload['body'])
        self.assertNotIn('secret', payload['body'])

    def test_duplicate_remote_keys_stop_sync(self):
        with self.assertRaises(FeishuError):
            index_records([{'fields': {'同步键': 'x'}}, {'fields': {'同步键': 'x'}}])


if __name__ == '__main__':
    unittest.main()
