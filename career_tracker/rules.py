"""Conservative fallback analysis for unattended runs without an API key."""
from __future__ import annotations

import re

REJECT = re.compile(r'不合适|未通过|感谢.*申请|遗憾|淘汰|rejected', re.I)
INTERVIEW = re.compile(r'面试|interview', re.I)
WRITTEN = re.compile(r'笔试|在线考试|written test|coding test', re.I)
ASSESSMENT = re.compile(r'测评|assessment|性格测试', re.I)
SUBMITTED = re.compile(r'投递成功|申请成功|简历已收到|更新简历|已投递|application received', re.I)
BRACKET_COMPANY = re.compile(r'[【\[]([^】\]]{2,30})[】\]]')
ROLE = re.compile(r'([\u4e00-\u9fffA-Za-z0-9/（）() -]{2,40}(?:测试开发|测试工程师|软件测试|开发工程师|工程师|实习生|岗位))')


def analyze(batch):
    results = []
    for mail in batch['messages']:
        text = mail['subject'] + '\n' + mail['body']
        if REJECT.search(text):
            stage, event, action, review = '已拒绝', '收到淘汰通知', '已淘汰', ''
        elif INTERVIEW.search(text):
            stage, event, action, review = '待面试', '收到面试相关通知', '按邮件要求参加或确认面试', '具体时间需人工核对'
        elif WRITTEN.search(text):
            stage, event, action, review = '待笔试', '收到笔试相关通知', '按邮件要求完成笔试', '截止时间需人工核对'
        elif ASSESSMENT.search(text):
            stage, event, action, review = '待笔试', '收到测评相关通知', '按邮件要求完成测评', '截止时间需人工核对'
        elif SUBMITTED.search(text):
            stage, event, action, review = '已投递', '收到投递确认', '', ''
        else:
            stage, event, action, review = '待确认', '收到待确认招聘邮件', '人工查看邮件正文', '规则无法可靠判断流程阶段'
        company = (BRACKET_COMPANY.search(mail['subject']) or [None, ''])[1].strip()
        role_match = ROLE.search(text[:5000])
        role = role_match.group(1).strip() if role_match else ''
        review_reason = '；'.join(x for x in (review, '未可靠识别公司或岗位' if not company or not role else '',
                                               '正文截断' if mail.get('truncated') else '') if x)
        event_data = {'company': company, 'role': role, 'recruitment': '', 'event': event,
                      'effective_at': mail['sent'], 'stage': stage, 'next_action': action,
                      'evidence': mail['subject'][:500] or event, 'needs_review': bool(review_reason),
                      'review_reason': review_reason}
        results.append({'message_id': mail['message_id'], 'relevant': True, 'reason': review or event,
                        'events': [event_data]})
    return {'batch_id': batch['batch_id'], 'results': results}
