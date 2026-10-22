# coding=utf-8
"""完整 Django + 原 PostgreSQL + 真实本机向量检索；LLM 明确为测试替身。

必须显式 P7A_REAL_DB=1，并加载现有部署配置；事务回滚本测试的独立 Chat/记忆。
不迁移、不清库，不修改原机器人设置。独立执行，不能与 settings.configure 的隔离测试混跑。
"""

# Django 必须先setup再导入ORM和真实pipeline。
# ruff: noqa: E402
import os
import sys
import time
import json
import uuid
import unittest
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
if os.getenv("P7A_REAL_DB") != "1":
    raise SystemExit("需显式 P7A_REAL_DB=1；本测试要求完整Django与真实数据库")
import django

django.setup()
from django.db import transaction, connection
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from application.models import Application, Chat, ChatRecord
from application.models import ApplicationLongTermMemory
from application.chat_pipeline.pipeline_manage import PipelineManage
from application.chat_pipeline.step.chat_step.impl.base_chat_step import BaseChatStep
from application.chat_pipeline.step.search_dataset_step.impl.base_search_dataset_step import BaseSearchDatasetStep
from application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step import BaseResetProblemStep
from common.handle.impl.response.openai_to_response import OpenaiToResponse
from common.handle.dpin_actual_usage import ActualUsageUnavailable
from models_provider.models import Model
from knowledge.models import Knowledge
from maxkb.const import CONFIG


class ModelStub:
    def __init__(self, purpose, prompt, completion, missing=False):
        self.purpose, self.prompt, self.completion, self.missing = purpose, prompt, completion, missing

    def invoke(self, messages):
        return AIMessage(
            content="替身回复",
            id=str(uuid.uuid4()),
            usage_metadata=None
            if self.missing
            else {
                "input_tokens": self.prompt,
                "output_tokens": self.completion,
                "total_tokens": self.prompt + self.completion,
                "input_token_details": {"cache_read": 0},
            },
        )


class DjangoUsageTests(unittest.TestCase):
    def setUp(self):
        self.atomic = transaction.atomic()
        self.atomic.__enter__()
        self.addCleanup(self.rollback_test)
        # 原 get_model_by_id 主动归还池连接；测试事务需保持连接直至回滚。
        self.keep_connection = patch.object(connection, "close")
        self.keep_connection.start()
        self.addCleanup(self.keep_connection.stop)
        self.app = Application.objects.get(id="01a0dbc2-4722-7681-9fbc-4ddf277cbbd6")
        self.user = str(uuid.uuid4())
        self.chat = Chat.objects.create(application=self.app, chat_user_id=self.user, abstract="P7a隔离测试")
        self.manage = PipelineManage([], base_to_response=OpenaiToResponse(), debug=True)
        self.manage.context.update(
            start_time=time.time(),
            workspace_id=self.app.workspace_id,
            application_id=str(self.app.id),
            model_id=str(self.app.model_id),
            chat_user_id=self.user,
        )

    def rollback_test(self):
        transaction.set_rollback(True)
        self.atomic.__exit__(None, None, None)

    def search(self):
        self.search_step = BaseSearchDatasetStep()
        knowledge = Knowledge.objects.first()
        return self.search_step.execute(
            "工作地点",
            [str(knowledge.id)],
            [],
            [],
            3,
            0.0,
            search_mode="embedding",
            workspace_id=self.app.workspace_id,
            manage=self.manage,
        )

    def reply(self, missing_memory=False, fail_save=False):
        self.manage.context["actual_memory_config"] = {
            "trigger_type": "ROUND",
            "trigger_setting": {"rounds": 1},
            "model_id": str(self.app.model_id),
            "model_params": {},
        }
        paragraph_list = self.search()
        self.assertGreater(len(paragraph_list), 0)
        optimizer = BaseResetProblemStep()
        with patch(
            "application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step.get_model_instance_by_model_workspace_id",
            return_value=ModelStub("optimizer", 1, 2),
        ):
            optimizer.execute("问题", [], str(self.app.model_id), manage=self.manage)

        def persist(chat_id, record_id, paragraphs, problem, answer, manage, step, *args, **kwargs):
            if fail_save:
                raise RuntimeError("受控持久化失败")
            ChatRecord.objects.create(
                id=record_id,
                chat_id=chat_id,
                problem_text=problem,
                answer_text=answer,
                index=1,
                message_tokens=manage.context["message_tokens"],
                answer_tokens=manage.context["answer_tokens"],
                details={"calls": manage.context["actual_usage_calls"], "retrieval": manage.context["retrieval_usage"]},
            )

        with patch(
            "application.long_term_memory.get_model_instance_by_model_workspace_id",
            return_value=ModelStub("memory", 7, 8, missing_memory),
        ):
            step = BaseChatStep()
            step.context["start_time"] = time.time()
            return step.execute_block(
                [SystemMessage(content="记忆：{memory}"), HumanMessage(content="问题")],
                str(self.chat.id),
                "问题",
                SimpleNamespace(handler=persist),
                chat_model=ModelStub("reply", 3, 4),
                paragraph_list=paragraph_list,
                manage=self.manage,
                chat_user_id=self.user,
                no_references_setting={"status": "ai_questioning"},
                model_setting={},
            )

    def test_local_embedding_real_query_and_all_calls_persist(self):
        response = self.reply()
        self.assertEqual(response.status_code, 200)
        usage = json.loads(response.content)["usage"]
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]), (11, 14, 25))
        self.assertEqual([c["purpose"] for c in usage["calls"]], ["problem_optimization", "reply", "long_term_memory"])
        self.assertEqual(usage["retrieval"]["vector_dimensions"], 768)
        self.assertEqual(usage["retrieval"]["token_usage"], "not_applicable")
        self.assertEqual(ChatRecord.objects.get(chat=self.chat).message_tokens, 11)
        self.assertTrue(
            ApplicationLongTermMemory.objects.filter(application_id=self.app.id, chat_user_id=self.user).exists()
        )
        print(
            json.dumps(
                {
                    "kind": "full_django_real_pg_local_embedding_fake_llm",
                    "calls": usage["calls"],
                    "retrieval": usage["retrieval"],
                    "record_persisted": True,
                },
                ensure_ascii=False,
            )
        )

    def test_remote_provider_and_remote_local_server_fail_before_model_construction(self):
        local = Model.objects.get(id=Knowledge.objects.first().embedding_model_id)
        for field, value in [("provider", "model_openai_provider"), ("host", "remote.example.invalid")]:
            remote = SimpleNamespace(
                model_type="EMBEDDING", provider=value if field == "provider" else local.provider, name="测试"
            )
            with (
                patch(
                    "application.chat_pipeline.step.search_dataset_step.impl.base_search_dataset_step.get_model_by_id",
                    return_value=remote,
                ),
                patch(
                    "application.chat_pipeline.step.search_dataset_step.impl.base_search_dataset_step.ModelManage.get_model"
                ) as build,
                patch.dict(CONFIG, {"LOCAL_MODEL_HOST": value if field == "host" else "127.0.0.1"}),
            ):
                with self.assertRaises(ActualUsageUnavailable):
                    self.search()
                build.assert_not_called()

    def test_missing_memory_usage_has_no_sendable_reply_or_chat_record(self):
        response = self.reply(missing_memory=True)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ChatRecord.objects.filter(chat=self.chat).exists())
        self.assertFalse(
            ApplicationLongTermMemory.objects.filter(application_id=self.app.id, chat_user_id=self.user).exists()
        )

    def test_chat_save_failure_rolls_back_pending_memory(self):
        response = self.reply(fail_save=True)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(ChatRecord.objects.filter(chat=self.chat).exists())
        self.assertFalse(
            ApplicationLongTermMemory.objects.filter(application_id=self.app.id, chat_user_id=self.user).exists()
        )
        print(
            json.dumps(
                {
                    "kind": "fake_llm_failure_side_effect",
                    "status": 502,
                    "chat_record_saved": False,
                    "memory_rollback_with_chat": True,
                    "test_transaction_rolled_back": True,
                }
            )
        )

    def test_optimizer_missing_model_rejects_without_estimation(self):
        with patch(
            "application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step.get_model_instance_by_model_workspace_id",
            return_value=None,
        ):
            with self.assertRaises(ActualUsageUnavailable):
                BaseResetProblemStep().execute("问题", [], str(self.app.model_id), manage=self.manage)



class ScopeDjangoTests(unittest.TestCase):
    """真实 PG/缓存/向量检索；仅模型 invoke 是替身。独立新身份提交后定向清理。"""
    def setUp(self):
        from application.models import ChatUserType
        from chat.serializers.chat import OpenAIChatSerializer
        self.serializer = OpenAIChatSerializer
        self.app = Application.objects.get(id="01a0dbc2-4722-7681-9fbc-4ddf277cbbd6")
        self.scopes = [str(uuid.uuid5(uuid.NAMESPACE_URL, str(uuid.uuid4()))) for _ in range(2)]
        self.user_type = ChatUserType.APPLICATION_API_KEY.value
        self.seen = []
        self.memory_inputs = []
        self.guard = threading.Lock()
        owner = self

        class Spy(ModelStub):
            def invoke(self, messages):
                text = '\n'.join(str(m.content) for m in messages)
                with owner.guard:
                    (owner.memory_inputs if self.purpose == 'memory' else owner.seen).append(text)
                result = super().invoke(messages)
                if self.purpose == 'memory':
                    # 保留测试标记，以验证下一次提取含旧记忆和本轮历史。
                    result.content = '\n'.join(marker for marker in ('作用域甲', '作用域乙', '甲第二轮', '甲第三轮', '甲第四轮') if marker in text)
                return result

        self.patches = [
            patch('application.chat_pipeline.step.chat_step.impl.base_chat_step.get_model_instance_by_model_workspace_id', return_value=Spy('reply', 3, 4)),
            patch('application.long_term_memory.get_model_instance_by_model_workspace_id', return_value=Spy('memory', 7, 8)),
            patch('chat.serializers.chat.request_memory_config', return_value={'trigger_type': 'ROUND', 'trigger_setting': {'rounds': 2}, 'model_id': str(self.app.model_id), 'model_params': {}}),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        from application.models import ApplicationChatUserStats
        from application.serializers.common import ChatInfo
        from django.core.cache import cache
        from common.constants.cache_version import Cache_Version
        for item in reversed(self.patches):
            item.stop()
        ids = list(Chat.objects.filter(application=self.app, chat_user_id__in=self.scopes).values_list('id', flat=True))
        with transaction.atomic():
            Chat.objects.filter(id__in=ids).delete()
            ApplicationLongTermMemory.objects.filter(application=self.app, chat_user_id__in=self.scopes).delete()
            ApplicationChatUserStats.objects.filter(application=self.app, chat_user_id__in=self.scopes).delete()
        for cid in ids:
            cache.delete(Cache_Version.CHAT.get_key(key=str(cid)), version=Cache_Version.CHAT_INFO.get_version())
        connection.close()

    def reply(self, scope, text, chat_id=None):
        instance = {'messages': [{'role': 'user', 'content': text}], 'stream': False}
        if chat_id:
            instance['chat_id'] = chat_id
        try:
            response = self.serializer(data={'application_id': str(self.app.id), 'chat_user_id': scope,
                                      'chat_user_type': self.user_type, 'ip_address': '127.0.0.1',
                                      'source': {'type': 'API_CALL', 'dpin_scope_version': 1}}).chat(instance)
            self.assertEqual(response.status_code, 200, response.content[:120])
            body = json.loads(response.content)
            self.assertEqual(body['dpin_scope'], scope)
            return body['choices'][0]['chat_id'], body['usage']
        finally:
            connection.close()

    def test_isolation_cache_reopen_and_legacy_chat_rejection(self):
        from application.serializers.common import ChatInfo
        from common.handle.dpin_scope import DpinConversationConflict
        from django.core.cache import cache
        from common.constants.cache_version import Cache_Version
        a, b = self.scopes
        legacy_identity = str(uuid.uuid4())
        self.scopes.append(legacy_identity)
        old_memory = ApplicationLongTermMemory.objects.create(application=self.app, chat_user_id=legacy_identity,
                                                              memory='旧共享记忆保留标记，不得归入甲或乙')
        ca, _ = self.reply(a, '作用域甲')
        cb, _ = self.reply(b, '作用域乙')
        self.assertNotEqual(ca, cb)
        ca2, ua = self.reply(a, '甲第二轮', ca)
        cb2, ub = self.reply(b, '作用域乙第二轮', cb)
        self.assertEqual((ca2, cb2), (ca, cb))
        self.assertEqual([c['purpose'] for c in ua['calls']], ['reply', 'long_term_memory'])
        ma = ApplicationLongTermMemory.objects.get(application=self.app, chat_user_id=a).memory
        mb = ApplicationLongTermMemory.objects.get(application=self.app, chat_user_id=b).memory
        self.assertIn('作用域甲', ma)
        self.assertNotIn('作用域乙', ma)
        self.assertIn('作用域乙', mb)
        self.assertNotIn('作用域甲', mb)
        old_memory.refresh_from_db()
        self.assertEqual(old_memory.memory, '旧共享记忆保留标记，不得归入甲或乙')
        self.assertTrue(all('旧共享记忆保留标记' not in text for text in self.seen + self.memory_inputs))
        # 重新打开从数据库恢复，显式清掉此会话缓存。
        cache.delete(Cache_Version.CHAT.get_key(key=ca), version=Cache_Version.CHAT_INFO.get_version())
        self.assertEqual(self.reply(a, '甲第三轮', ca)[0], ca)
        self.assertIn('作用域甲', self.seen[-1])
        self.assertNotIn('作用域乙', self.seen[-1])
        before = len(self.seen)
        with self.assertRaises(DpinConversationConflict):
            self.reply(b, '越界不应调用模型', ca)
        self.assertEqual(len(self.seen), before)
        legacy = Chat.objects.create(application=self.app, chat_user_id=a, source={'type': 'API_CALL'})
        with self.assertRaises(DpinConversationConflict):
            self.reply(a, '旧共享会话不应被接管', str(legacy.id))
        self.assertEqual(len(self.seen), before)
        self.assertEqual(list(ChatRecord.objects.filter(chat_id=ca).order_by('index').values_list('index', flat=True)), [1, 2, 3])

    def test_same_scope_concurrent_round_and_memory_merge(self):
        a = self.scopes[0]
        ca, _ = self.reply(a, '作用域甲')
        barrier = threading.Barrier(2)
        def call(text):
            barrier.wait(timeout=10)
            return self.reply(a, text, ca)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(call, text) for text in ('甲第二轮', '甲第三轮')]
            results = [future.result(timeout=80) for future in futures]
        self.assertEqual([result[0] for result in results], [ca, ca])
        self.assertEqual(sum(len(result[1]['calls']) for result in results), 3)
        self.assertEqual(len(self.memory_inputs), 1)
        # 第四轮另开 chat_id，ROUND 计数及旧记忆仍按相同可信身份延续。
        new_chat, usage = self.reply(a, '甲第四轮')
        self.assertNotEqual(ca, new_chat)
        self.assertEqual(len(usage['calls']), 2)
        self.assertEqual(len(self.memory_inputs), 2)
        for marker in ('作用域甲', '甲第二轮', '甲第三轮', '甲第四轮'):
            self.assertIn(marker, self.memory_inputs[-1])
        self.assertEqual(list(ChatRecord.objects.filter(chat_id=ca).order_by('index').values_list('index', flat=True)), [1, 2, 3])
        self.assertEqual(list(ChatRecord.objects.filter(chat_id=new_chat).values_list('index', flat=True)), [1])
        self.assertEqual([r.message_tokens for r in ChatRecord.objects.filter(chat_id=ca).order_by('index')], [3, 10, 3])

    def test_advisory_lock_is_cross_connection_and_released_after_error(self):
        import psycopg
        from common.handle.dpin_scope import serialized_dpin_scope
        a, b = self.scopes
        cfg = connection.settings_dict
        def key(scope):
            return int.from_bytes(hashlib.sha256((str(self.app.id) + ':' + scope).encode()).digest()[:8], 'big', signed=True)
        with psycopg.connect(dbname=cfg['NAME'], host=cfg['HOST'], port=cfg['PORT'], user=cfg['USER'], password=cfg['PASSWORD'], autocommit=True) as probe:
            with self.assertRaisesRegex(RuntimeError, '受控退出'):
                with serialized_dpin_scope(str(self.app.id), a), probe.cursor() as cursor:
                    cursor.execute('SELECT pg_try_advisory_lock(%s)', (key(a),))
                    self.assertFalse(cursor.fetchone()[0])
                    cursor.execute('SELECT pg_try_advisory_lock(%s)', (key(b),))
                    self.assertTrue(cursor.fetchone()[0])
                    cursor.execute('SELECT pg_advisory_unlock(%s)', (key(b),))
                    raise RuntimeError('受控退出')
            with probe.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (key(a),))
                self.assertTrue(cursor.fetchone()[0])
                cursor.execute('SELECT pg_advisory_unlock(%s)', (key(a),))


if __name__ == "__main__":
    with connection.cursor() as c:
        c.execute("select version()")
        print(json.dumps({"django": django.get_version(), "db": c.fetchone()[0]}))
    unittest.main(verbosity=2)
