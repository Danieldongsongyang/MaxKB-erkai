# coding=utf-8
"""执行 C2 真实上下文／流水线函数，模型和数据库为明确隔离夹具。"""
import hashlib
import json
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from test_dpin_pipeline_usage import (ns, load_definitions, fixture, state, Query, Memory, ChatModel,
                                      BaseChatStep, PipelineManage, OpenaiToResponse, SystemMessage)
from common.handle.dpin_reply_context import (parse_reply_context, candidate_memory_facts, reply_problem,
                                              SYSTEM_PROMPT, PROTOCOL)
from common.exception.app_exception import AppApiException


def key(text):
    return hashlib.sha256(text.encode()).hexdigest()


def envelope():
    return {'protocol': PROTOCOL, 'history_mode': 'channel_snapshot', 'current_key': key('new'), 'materials': {},
            'messages': [{'key': key('q'), 'role': 'recruiter', 'text': '城市？'},
                         {'key': key('new'), 'role': 'candidate', 'text': '我在上海'}],
            'responsibility': {'protocol': PROTOCOL, 'allow_new_collection': False, 'auto_advance': False,
                               'questions': [{'index': 0, 'question': '城市？', 'question_type': 'collect_only',
                                              'anchor_keys': [key('q')]}]}}


class C2ContextTests(unittest.TestCase):
    def test_validated_scope_and_server_derived_memory_version(self):
        scope = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'trusted'))
        context = parse_reply_context(json.dumps(envelope()), scope)
        self.assertNotEqual(context['memory_user_id'], scope)
        self.assertEqual(context['memory_user_id'], parse_reply_context(json.dumps(envelope()), scope)['memory_user_id'])
        self.assertNotIn('memory_user_id', reply_problem(context))
        for mutate in [lambda e: e.update(current_key=key('q')),
                       lambda e: e['messages'].append(e['messages'][0]),
                       lambda e: e['messages'][0].update(role='unknown'),
                       lambda e: e['responsibility'].update(allow_new_collection=True),
                       lambda e: e['responsibility']['questions'][0].update(anchor_keys=[key('missing')])]:
            invalid = envelope(); mutate(invalid)
            with self.assertRaises(AppApiException): parse_reply_context(json.dumps(invalid), scope)
        with self.assertRaises(AppApiException): parse_reply_context(json.dumps(envelope()))

    def test_main_reply_and_optimizer_exclude_all_generated_history(self):
        context = parse_reply_context(json.dumps(envelope()), str(uuid.uuid5(uuid.NAMESPACE_DNS, 'scope')))
        current = fixture(); state.set(current)
        namespace = {**ns, 'IGenerateHumanMessageStep': object, 'SYSTEM_PROMPT': SYSTEM_PROMPT, 'reply_problem': reply_problem}
        load_definitions('apps/application/chat_pipeline/step/generate_human_message_step/impl/base_generate_human_message_step.py', {'BaseGenerateHumanMessageStep'}, namespace)
        old = SimpleNamespace(get_human_message=lambda: '旧生成输入', get_ai_message=lambda: '未发送建议-泄漏标记')
        manage = PipelineManage([], base_to_response=OpenaiToResponse()); manage.context['dpin_reply_context'] = context
        messages = namespace['BaseGenerateHumanMessageStep']().execute('当前输入', [], [old], 5, 1000, '{question}',
                      no_references_setting={'status': 'ai_questioning', 'value': '{question}'}, system='岗位知识', manage=manage)
        text = '\n'.join(message.content for message in messages)
        self.assertNotIn('泄漏标记', text); self.assertIn('我在上海', text); self.assertIn('禁止新增采集', text)
        ns['BaseResetProblemStep']().execute('原输入', [old], 'optimizer', manage=manage)
        optimized = '\n'.join(message.content for message in current.models['optimizer'].inputs[0])
        self.assertIn('我在上海', optimized); self.assertNotIn('泄漏标记', optimized)

    def test_memory_trigger_uses_candidate_facts_only_and_separate_version(self):
        for enabled in [False, True]:
            for history in [0, 1]:
                current = fixture(rounds=2, history=history); state.set(current)
                current.records = [SimpleNamespace(problem_text='未发输入-旧标记', answer_text='未发建议-旧标记') for _ in range(history)]
                scope = str(uuid.uuid5(uuid.NAMESPACE_DNS, 'scope'))
                context = parse_reply_context(json.dumps(envelope()), scope)
                filters = []
                class ScopedQuery(Query):
                    def filter(self, **kwargs):
                        if self.model is Memory: filters.append(kwargs)
                        return super().filter(**kwargs)
                    def first(self):
                        return None if self.model is Memory else super().first()
                manage = PipelineManage([], base_to_response=OpenaiToResponse())
                manage.context.update(start_time=time.time(), workspace_id='workspace', application_id='app', model_id='reply',
                                      chat_user_id=scope, dpin_scope=scope, dpin_reply_context=context,
                                      actual_memory_config=ns['request_memory_config'](current.application, scope) if enabled else None)
                step = BaseChatStep(); model = ChatModel('reply', 3, 4, '本轮未发生成-新标记')
                with patch.dict(ns, QuerySet=ScopedQuery, candidate_memory_facts=candidate_memory_facts):
                    result = step.execute_block([SystemMessage(content='记忆：{memory}')], 'chat', '本轮输入-新标记',
                          SimpleNamespace(handler=lambda *a, **k: None), chat_model=model, paragraph_list=[], manage=manage,
                          chat_user_id=scope, no_references_setting={'status': 'ai_questioning'}, model_setting={})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(json.loads(result.content)['reply_protocol'], PROTOCOL)
                triggered = enabled and history == 1
                self.assertEqual(len(current.models['memory'].inputs), int(triggered))
                if triggered:
                    prompt = current.models['memory'].inputs[0][0].content
                    self.assertIn('我在上海', prompt); self.assertNotIn('旧标记', prompt); self.assertNotIn('新标记', prompt)
                    self.assertNotIn('城市？', prompt)
                    self.assertEqual(manage.context['pending_memory']['chat_user_id'], context['memory_user_id'])
                if enabled: self.assertEqual(filters[0]['chat_user_id'], context['memory_user_id'])


if __name__ == '__main__':
    unittest.main()
