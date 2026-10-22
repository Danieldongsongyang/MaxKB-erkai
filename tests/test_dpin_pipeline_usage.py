# coding=utf-8
"""执行源码中的真实函数，注入模型与数据库夹具；不导入生产 Django settings。

AST 仅选择被测定义并跳过启动时依赖，定义本身不改写；HTTP 响应和消息使用真实依赖。
此测试不替代完整 Django 启动、真实数据库和应用验收。
"""
import ast
import copy
import json
import re
import sys
import time
import unittest
import uuid
from abc import abstractmethod
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from contextlib import nullcontext
from functools import reduce
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

from django.conf import settings
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage
from rest_framework import status

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps'))
if not settings.configured:
    settings.configure(USE_TZ=True, DEFAULT_CHARSET='utf-8', SECRET_KEY='isolated-test')

from common.handle.dpin_actual_usage import ActualUsageUnavailable, RequestActualUsage, actual_token_usage  # noqa: E402
from common.handle.impl.response.openai_to_response import OpenaiToResponse  # noqa: E402


def load_definitions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [n for n in tree.body if getattr(n, 'name', None) in names
             or isinstance(n, ast.Assign) and any(getattr(t, 'id', None) in names for t in n.targets)]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                              *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), 'exec'), namespace)


def load_class_methods(path, name, methods, namespace):
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                              node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / path), 'exec'), namespace)


state = ContextVar('test_dpin_state')


class Memory:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def save(self):
        current = state.get()
        current.memory = self
        current.events.append('memory_saved')


class Record:
    pass


class Query:
    def __init__(self, model):
        self.model = model

    def filter(self, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def only(self, *args):
        return self

    def count(self):
        return len(state.get().records)

    def first(self):
        return state.get().memory if self.model is Memory else state.get().application

    def __getitem__(self, item):
        return list(reversed(state.get().records))[item]


class ChatModel:
    # 故意携带不相干共享用量，严格路径不得读取它。
    usage_metadata = {'input_tokens': 999999, 'output_tokens': 999999}

    def __init__(self, purpose, prompt, completion, content, failure=None, entered=None, release=None):
        self.purpose, self.prompt, self.completion, self.content = purpose, prompt, completion, content
        self.failure, self.entered, self.release = failure, entered, release
        self.inputs = []

    def invoke(self, messages):
        self.inputs.append(messages)
        state.get().events.append(self.purpose)
        if self.entered:
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('测试等待超时')
        if self.failure == 'exception':
            raise RuntimeError('敏感上游错误不得返回')
        usage = None if self.failure == 'missing' else {
            'input_tokens': self.prompt, 'output_tokens': self.completion, 'total_tokens': self.prompt + self.completion, 'input_token_details': {'cache_read': 0}}
        response = {'token_usage': {'prompt_tokens': self.prompt + 1, 'completion_tokens': self.completion}}
        return AIMessage(content=self.content, usage_metadata=usage, id=self.purpose,
                         response_metadata=response if self.failure == 'inconsistent' else {})

    def stream(self, messages):
        state.get().events.append('legacy_stream')
        yield AIMessageChunk(content=self.content)

    def get_num_tokens(self, *args):
        raise AssertionError('严格路径禁止估算')

    def get_num_tokens_from_messages(self, *args):
        raise AssertionError('严格路径禁止估算')


class Step:
    def __init__(self):
        self.context = {'start_time': time.time(), 'step_args': {'message_list': []}}
        self.status = 200
        self.err_message = ''


async_extract = SimpleNamespace(apply_async=Mock())
Memory.objects = SimpleNamespace(update_or_create=lambda **kw: Memory(**kw['defaults']).save())
ns = dict(transaction=SimpleNamespace(atomic=nullcontext), nullcontext=nullcontext, time=time, re=re, deepcopy=copy.deepcopy, reduce=reduce, status=status, abstractmethod=abstractmethod,
          uuid=SimpleNamespace(uuid7=uuid.uuid4), IChatStep=Step, IResetProblemStep=Step,
          AIMessage=AIMessage, AIMessageChunk=AIMessageChunk, HumanMessage=HumanMessage, SystemMessage=SystemMessage,
          ActualUsageUnavailable=ActualUsageUnavailable, RequestActualUsage=RequestActualUsage,
          actual_token_usage=actual_token_usage, OpenaiToResponse=OpenaiToResponse,
          SystemToResponse=type('SystemToResponse', (), {}), QuerySet=Query, ApplicationLongTermMemory=Memory,
          ChatRecord=Record, get_runtime_user_id=lambda **kwargs: 'user',
          filter_authorized_ids=lambda *args, **kwargs: [], extract_long_term_memory=async_extract,
          _=lambda value: value, flat_map=lambda values: [item for group in values for item in group],
          prompt='优化{question}', get_model_instance_by_model_workspace_id=lambda model_id, *args, **kwargs:
          state.get().models[model_id], add_access_num=Mock())
load_definitions('apps/application/flow/tools.py', {'Reasoning'}, ns)
load_definitions('apps/application/long_term_memory/__init__.py',
                 {'_get_long_term_config', 'request_memory_config', 'extract_long_term_memory_for_request',
                  '_run_extract', 'long_term_prompt'}, ns)
load_definitions('apps/application/chat_pipeline/pipeline_manage.py', {'PipelineManage'}, ns)
load_definitions('apps/application/chat_pipeline/I_base_chat_pipeline.py', {'IBaseChatPipelineStep'}, ns)
load_definitions('apps/application/chat_pipeline/step/reset_problem_step/impl/base_reset_problem_step.py',
                 {'BaseResetProblemStep'}, ns)
load_definitions('apps/application/chat_pipeline/step/chat_step/impl/base_chat_step.py',
                 {'BaseChatStep', 'write_context'}, ns)
BaseChatStep, PipelineManage = ns['BaseChatStep'], ns['PipelineManage']
# 空工具配置的受控路径替身；工具消费者本身属于明确拒绝范围。
BaseChatStep._handle_mcp_request = lambda *args, **kwargs: None


class Serializer:
    def __init__(self, **kwargs):
        pass

    def is_valid(self, **kwargs):
        return True


ns.update(serializers=SimpleNamespace(Serializer=Serializer), ChatMessageSerializers=Serializer,
          ApplicationTypeChoices=SimpleNamespace(SIMPLE='SIMPLE'), SystemToResponse=ns['SystemToResponse'],
          BaseToResponse=object)
load_class_methods('apps/chat/serializers/chat.py', 'ChatSerializers', {'chat', 'chat_simple'}, ns)


def fixture(rounds=1, history=0, memory_model=None):
    application = SimpleNamespace(type='SIMPLE', id='app', long_term_enable=True, long_term_trigger_type='ROUND',
                                  long_term_trigger_setting={'rounds': rounds}, long_term_model_id='memory',
                                  long_term_model_params_setting={})
    return SimpleNamespace(application=application, records=[SimpleNamespace(problem_text='旧问题', answer_text='旧回复')
                                                            for _ in range(history)],
                           memory=Memory(memory='已有记忆'), events=[],
                           models={'memory': memory_model or ChatModel('memory', 7, 8, '更新记忆'),
                                   'optimizer': ChatModel('optimizer', 1, 2, '<data>优化问题</data>')})


def run_reply(current, model=None, optimize=False, response=None):
    state.set(current)
    manage = PipelineManage([], base_to_response=response or OpenaiToResponse())
    manage.context.update(start_time=time.time(), workspace_id='workspace', application_id='app', model_id='reply',
                          chat_user_id='user', actual_memory_config=ns['request_memory_config'](current.application, 'user'))
    if optimize:
        optimizer = ns['BaseResetProblemStep']()
        optimizer.execute('问题', [], 'optimizer', workspace_id='workspace', manage=manage)
    step = BaseChatStep()
    original_system = SystemMessage(content='记忆：{memory}')

    def persist(*args, **kwargs):
        current.events.append('persist')
        # 真实 get_details 连续调用，不能重复提取或投递 Celery。
        step.get_details(manage)
        step.get_details(manage)
        current.persisted = (manage.context['message_tokens'], manage.context['answer_tokens'])

    result = step.execute_block([original_system, HumanMessage(content='问题')], 'chat', '问题',
                                SimpleNamespace(handler=persist), chat_model=model or ChatModel('reply', 3, 4, '主回复'),
                                paragraph_list=[], manage=manage, chat_user_id='user',
                                no_references_setting={'status': 'ai_questioning'}, model_setting={})
    current.system = original_system
    return result, manage


class PipelineUsageTests(unittest.TestCase):
    def setUp(self):
        async_extract.apply_async.reset_mock()

    def test_main_optimizer_memory_total_persistence_and_no_duplicate_task(self):
        current = fixture()
        reply_model = ChatModel('reply', 3, 4, '主回复')
        result, manage = run_reply(current, model=reply_model, optimize=True)
        self.assertEqual(result.status_code, 200)
        data = json.loads(result.content)
        self.assertEqual((data['usage']['prompt_tokens'], data['usage']['completion_tokens']), (11, 14))
        self.assertEqual(data['usage']['total_tokens'], 25)
        self.assertEqual(data['usage']['usage_source'], 'upstream')
        self.assertEqual(len(data['usage']['calls']), 3)
        self.assertEqual(current.persisted, (11, 14))
        self.assertEqual(current.events, ['optimizer', 'reply', 'memory', 'memory_saved', 'persist'])
        self.assertEqual(current.system.content, '记忆：{memory}')
        self.assertEqual(reply_model.inputs[0][0].content, '记忆：已有记忆')
        self.assertEqual(manage.actual_usage.finalize(), (11, 14))
        async_extract.apply_async.assert_not_called()
        self.assertIn('用户：问题\nAI：主回复', current.models['memory'].inputs[0][0].content)

    def test_round_trigger_includes_current_conversation_once(self):
        for history, expected_calls in [(0, 0), (1, 1), (2, 0), (3, 1)]:
            current = fixture(rounds=2, history=history)
            result, _ = run_reply(current)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(current.models['memory'].inputs), expected_calls)
            if expected_calls:
                prompt = current.models['memory'].inputs[0][0].content
                self.assertEqual(prompt.count('用户：问题'), 1)
                self.assertEqual(prompt.count('用户：旧问题'), 1)

    def test_missing_invalid_or_failed_extra_call_has_no_sendable_reply(self):
        for failure in ['missing', 'inconsistent', 'exception']:
            current = fixture(memory_model=ChatModel('memory', 7, 8, '更新记忆', failure=failure))
            result, manage = run_reply(current)
            self.assertEqual(result.status_code, 502)
            self.assertEqual(json.loads(result.content)['error']['code'], 'MAXKB_ACTUAL_USAGE_UNAVAILABLE')
            self.assertNotIn('choices', json.loads(result.content))
            self.assertNotIn('persist', current.events)
            self.assertNotIn('memory_saved', current.events)
            with self.assertRaises(ActualUsageUnavailable):
                manage.actual_usage.finalize()
        async_extract.apply_async.assert_not_called()

    def test_reply_failure_never_starts_memory(self):
        current = fixture()
        result, _ = run_reply(current, model=ChatModel('reply', 3, 4, '主回复', failure='missing'))
        self.assertEqual(result.status_code, 502)
        self.assertEqual(current.events, ['reply'])

    def test_optimizer_missing_usage_fails_before_main_and_does_not_persist(self):
        current = fixture()
        current.models['optimizer'].failure = 'missing'
        state.set(current)
        manage = PipelineManage([], base_to_response=OpenaiToResponse())

        class OptimizationStep(ns['IBaseChatPipelineStep']):
            def valid_args(self, manage):
                pass

            def _run(self, manage):
                ns['BaseResetProblemStep']().execute('问题', [], 'optimizer', manage=manage)

        persist = Mock()
        manage.step_list = [OptimizationStep()]
        manage.run({'post_response_handler': SimpleNamespace(handler=persist)})
        self.assertEqual(manage.context['chat_result'].status_code, 502)
        self.assertEqual(current.events, ['optimizer'])
        persist.assert_not_called()
        async_extract.apply_async.assert_not_called()

    def test_static_reply_has_no_actual_call_and_fails(self):
        current = fixture()
        state.set(current)
        manage = PipelineManage([], base_to_response=OpenaiToResponse())
        manage.context.update(start_time=time.time())
        handler = Mock()
        result = BaseChatStep().execute_block([], 'chat', '问题', SimpleNamespace(handler=handler),
                                              manage=manage, no_references_setting={'status': 'designated_answer',
                                                                                   'value': '静态回复'}, model_setting={})
        self.assertEqual(result.status_code, 502)
        handler.assert_not_called()
        self.assertEqual(current.events, [])

    def test_waits_for_memory_before_http_response(self):
        entered, release = Event(), Event()
        current = fixture(memory_model=ChatModel('memory', 7, 8, '更新记忆', entered=entered, release=release))
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_reply, current)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(future.done())
                self.assertNotIn('persist', current.events)
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3)[0].status_code, 200)

    def test_concurrent_requests_using_same_model_instance_are_isolated(self):
        shared = ChatModel('reply', 3, 4, '主回复')

        def request(index):
            current = fixture(memory_model=ChatModel('memory', index, 1, '更新记忆'))
            result, _ = run_reply(current, model=shared)
            data = json.loads(result.content)
            return data['usage']['prompt_tokens'], data['usage']['completion_tokens']

        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(list(executor.map(request, range(32))), [(i + 3, 5) for i in range(32)])
        async_extract.apply_async.assert_not_called()

    def test_configuration_is_copied_and_scheduled_is_rejected(self):
        current = fixture()
        state.set(current)
        config = ns['request_memory_config'](current.application, 'user')
        current.application.long_term_trigger_setting['rounds'] = 2
        self.assertEqual(config['trigger_setting']['rounds'], 1)
        current.application.long_term_trigger_type = 'SCHEDULED'
        with self.assertRaises(ActualUsageUnavailable):
            ns['request_memory_config'](current.application, 'user')
        for invalid in [True, 0, -1, 1.5, '1']:
            current.application.long_term_trigger_type = 'ROUND'
            current.application.long_term_trigger_setting = {'rounds': invalid}
            with self.assertRaises(ActualUsageUnavailable):
                ns['request_memory_config'](current.application, 'user')

    def test_response_rechecks_counts_and_cannot_trust_source_marker(self):
        response = OpenaiToResponse()
        usage = RequestActualUsage()
        state.set(fixture())
        usage.invoke('reply', ChatModel('reply', 3, 4, '主回复'), [])
        for prompt, completion, ledger in [(3, 4, None), (True, 4, usage), (3, -1, usage), (2, 4, usage)]:
            result = response.to_block_response('chat', 'record', '不能发送', True, prompt, completion,
                                                {'usage_source': 'upstream', 'actual_usage': ledger})
            self.assertEqual(result.status_code, 502)

    def test_response_publishes_aggregate_cache_facts(self):
        usage = RequestActualUsage()
        for purpose, hit in [('problem_optimization', 0), ('reply', 2), ('long_term_memory', 1)]:
            message = AIMessage(content='替身', usage_metadata={
                'input_tokens': 3, 'output_tokens': 1, 'total_tokens': 4,
                'input_token_details': {'cache_read': hit}})
            usage.complete(usage.begin(purpose), message)
        result = OpenaiToResponse().to_block_response(
            'chat', 'record', '替身', True, 9, 3,
            {'usage_source': 'upstream', 'actual_usage': usage})
        self.assertEqual(result.status_code, 200)
        returned = json.loads(result.content)['usage']
        self.assertEqual(returned['prompt_tokens_details']['cached_tokens'], 3)
        self.assertEqual(returned['cache_usage_source'], 'upstream')
        self.assertEqual([call['cached_tokens'] for call in returned['calls']], [0, 2, 1])

    def test_serializer_rejects_unknown_consumers_without_disabling_configuration(self):
        for unsupported in ['stream', 'workflow', 'mcp', 'tool', 'application', 'skill', 'scheduled']:
            current = fixture()
            app = current.application
            app.mcp_enable = app.tool_enable = app.application_enable = False
            app.skill_tool_ids = []
            info = SimpleNamespace(application=app, knowledge_id_list=[], get_application=lambda: app,
                                   get_chat_user=lambda **kwargs: None)
            instance = {'stream': False}
            if unsupported == 'stream':
                instance['stream'] = True
            elif unsupported == 'knowledge':
                info.knowledge_id_list = ['knowledge']
            elif unsupported == 'workflow':
                app.type = 'WORK_FLOW'
            elif unsupported in ['mcp', 'tool', 'application']:
                setattr(app, unsupported + '_enable', True)
            elif unsupported == 'skill':
                app.skill_tool_ids = ['skill']
            else:
                app.long_term_trigger_type = 'SCHEDULED'
            before = copy.deepcopy(vars(app))
            serializer = ns['ChatSerializers']()
            serializer.data = {'debug': True}
            serializer.get_chat_info = lambda: info
            serializer.is_valid_chat_id = lambda *args: None
            serializer.chat_simple = serializer.chat_work_flow = Mock(side_effect=AssertionError('不得进入模型调用'))
            result = serializer.chat(instance, OpenaiToResponse())
            self.assertEqual(result.status_code, 502)
            self.assertEqual(json.loads(result.content)['error']['code'], 'MAXKB_ACTUAL_USAGE_UNSUPPORTED')
            self.assertEqual(vars(app), before)

    def test_serializer_allows_round_memory_and_optimization(self):
        current = fixture()
        app = current.application
        app.mcp_enable = app.tool_enable = app.application_enable = False
        app.skill_tool_ids = []
        app.problem_optimization = True
        info = SimpleNamespace(application=app, knowledge_id_list=['local-knowledge'], get_application=lambda: app,
                               get_chat_user=lambda **kwargs: None)
        serializer = ns['ChatSerializers']()
        serializer.data = {'debug': True}
        serializer.get_chat_info = lambda: info
        serializer.is_valid_chat_id = lambda *args: None
        serializer.is_valid_application_simple = lambda **kwargs: None
        serializer.chat_simple = Mock(return_value='allowed')
        self.assertEqual(serializer.chat({'stream': False}, OpenaiToResponse()), 'allowed')

    def test_non_strict_memory_still_uses_existing_async_stream_path(self):
        current = fixture()
        state.set(current)
        ns['_run_extract']('workspace', 'app', 'user',
                           ns['request_memory_config'](current.application, 'user'), history_limit=1)
        self.assertEqual(current.events, [])  # 没有历史时，不进行提取。
        current.records.append(SimpleNamespace(problem_text='旧问题', answer_text='旧回复'))
        ns['_run_extract']('workspace', 'app', 'user',
                           ns['request_memory_config'](current.application, 'user'), history_limit=1)
        self.assertEqual(current.events, ['legacy_stream', 'memory_saved'])
        manage = PipelineManage([])
        manage.context['model_id'] = 'reply'
        BaseChatStep().get_details(manage)
        async_extract.apply_async.assert_called_once()


if __name__ == '__main__':
    unittest.main()
