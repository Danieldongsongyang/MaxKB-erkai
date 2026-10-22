# coding=utf-8
"""C2 完整 Django／原 PostgreSQL；模型为明确替身，独立会话事务回滚。"""
import os, sys, json, time, uuid, hashlib, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps'))
if os.getenv('C2_REAL_DB') != '1':
    raise SystemExit('需显式 C2_REAL_DB=1，并加载原部署环境')
import django
django.setup()
from django.db import connection, transaction
from application.models import Application, Chat, ChatRecord, ApplicationLongTermMemory
from application.chat_pipeline.pipeline_manage import PipelineManage
from application.chat_pipeline.step.generate_human_message_step.impl.base_generate_human_message_step import BaseGenerateHumanMessageStep
from application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step import BaseResetProblemStep
from application.chat_pipeline.step.chat_step.impl.base_chat_step import BaseChatStep
from common.handle.impl.response.openai_to_response import OpenaiToResponse
from common.handle.dpin_reply_context import parse_reply_context, PROTOCOL
from langchain_core.messages import AIMessage, SystemMessage

class Stub:
    def __init__(self, purpose, seen): self.purpose, self.seen = purpose, seen
    def invoke(self, messages):
        self.seen[self.purpose] = '\n'.join(str(x.content) for x in messages)
        return AIMessage(content='C2_NEW_MEMORY' if self.purpose == 'memory' else 'C2_CURRENT_UNSENT' if self.purpose == 'reply' else '工作地点',
                         usage_metadata={'input_tokens':3,'output_tokens':2,'total_tokens':5,'input_token_details':{'cache_read':0}})

class Tests(unittest.TestCase):
    def setUp(self):
        self.atomic=transaction.atomic(); self.atomic.__enter__()
        self.addCleanup(self.rollback)
        close=patch.object(connection,'close'); close.start(); self.addCleanup(close.stop)
        self.app=Application.objects.get(id='01a0dbc2-4722-7681-9fbc-4ddf277cbbd6')
        self.scope=str(uuid.uuid5(uuid.NAMESPACE_URL,str(uuid.uuid4())))
        self.chat=Chat.objects.create(application=self.app,chat_user_id=self.scope,abstract='C2事务测试',source={'type':'API_CALL','dpin_scope_version':1})
        self.old=ChatRecord.objects.create(chat=self.chat,problem_text='C2_OLD_PROBLEM',answer_text='C2_OLD_UNSENT',index=1)
        ApplicationLongTermMemory.objects.create(application=self.app,chat_user_id=self.scope,memory='C2_OLD_MEMORY')
        key=lambda s:hashlib.sha256(s.encode()).hexdigest()
        self.input={'protocol':PROTOCOL,'history_mode':'channel_snapshot','current_key':key('in2'),
                    'materials':{'job':{'city':'渠道岗位地点'}},'messages':[
                    {'key':key('in1'),'role':'candidate','text':'C2_CHANNEL_CANDIDATE'},
                    {'key':key('out'),'role':'recruiter','text':'C2_CONFIRMED_FIXED'},
                    {'key':key('in2'),'role':'candidate','text':'工作地点在哪里'}],
                    'responsibility':{'protocol':PROTOCOL,'allow_new_collection':False,'auto_advance':False,'questions':[]}}
        self.context=parse_reply_context(json.dumps(self.input),self.scope)
        self.assertNotEqual(self.context['memory_user_id'],self.scope)
    def rollback(self):
        transaction.set_rollback(True); self.atomic.__exit__(None,None,None)
    def run_case(self, memory_on):
        seen={}; manage=PipelineManage([],base_to_response=OpenaiToResponse(),debug=True)
        manage.context.update(start_time=time.time(),workspace_id=self.app.workspace_id,application_id=str(self.app.id),model_id=str(self.app.model_id),chat_user_id=self.scope,dpin_reply_context=self.context,
                              actual_memory_config={'trigger_type':'ROUND','trigger_setting':{'rounds':2},'model_id':str(self.app.model_id),'model_params':{}} if memory_on else None)
        with patch('application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step.get_model_instance_by_model_workspace_id',return_value=Stub('optimizer',seen)):
            BaseResetProblemStep().execute(json.dumps(self.input),[self.old],str(self.app.model_id),manage=manage)
        generator=BaseGenerateHumanMessageStep()
        messages=generator.execute(json.dumps(self.input),[],[self.old],10,10000,'{question}',padding_problem_text='C2_OPTIMIZED_ONLY',
                                   no_references_setting={'status':'ai_questioning','value':'{question}'},system='记忆：{memory}',manage=manage)
        def save(chat_id, record_id, paragraphs, problem, answer, manager, step, *args,**kwargs):
            ChatRecord.objects.create(id=record_id,chat_id=chat_id,problem_text=problem,answer_text=answer,index=2,
                                      message_tokens=manager.context['message_tokens'],answer_tokens=manager.context['answer_tokens'],details={'calls':manager.context['actual_usage_calls']})
        step=BaseChatStep(); step.context['start_time']=time.time()
        with patch('application.long_term_memory.get_model_instance_by_model_workspace_id',return_value=Stub('memory',seen)):
            response=step.execute_block(messages,str(self.chat.id),json.dumps(self.input),SimpleNamespace(handler=save),chat_model=Stub('reply',seen),paragraph_list=[],manage=manage,chat_user_id=self.scope,no_references_setting={'status':'ai_questioning'},model_setting={})
        self.assertEqual(response.status_code,200,response.content)
        data=json.loads(response.content); self.assertEqual(data['reply_protocol'],PROTOCOL)
        for purpose in ['reply','optimizer']:
            self.assertIn('C2_CHANNEL_CANDIDATE',seen[purpose]); self.assertIn('C2_CONFIRMED_FIXED',seen[purpose])
            for old in ['C2_OLD_PROBLEM','C2_OLD_UNSENT','C2_OLD_MEMORY','C2_OPTIMIZED_ONLY']: self.assertNotIn(old,seen[purpose])
        self.assertEqual(ApplicationLongTermMemory.objects.get(application=self.app,chat_user_id=self.scope).memory,'C2_OLD_MEMORY')
        self.assertEqual(ChatRecord.objects.get(id=self.old.id).answer_text,'C2_OLD_UNSENT')
        if memory_on:
            self.assertIn('C2_CHANNEL_CANDIDATE',seen['memory'])
            for excluded in ['C2_CONFIRMED_FIXED','C2_OLD_UNSENT','C2_CURRENT_UNSENT','C2_OLD_MEMORY']: self.assertNotIn(excluded,seen['memory'])
            self.assertEqual(ApplicationLongTermMemory.objects.get(application=self.app,chat_user_id=self.context['memory_user_id']).memory,'C2_NEW_MEMORY')
        else:
            self.assertNotIn('memory',seen); self.assertFalse(ApplicationLongTermMemory.objects.filter(application=self.app,chat_user_id=self.context['memory_user_id']).exists())
        self.assertEqual([c['purpose'] for c in data['usage']['calls']],['problem_optimization','reply']+(['long_term_memory'] if memory_on else []))
        print(json.dumps({'kind':'C2_full_django_original_pg_explicit_model_stub','memory_enabled':memory_on,'calls':data['usage']['calls'],'history_preserved':True,'old_memory_preserved':True,'memory_scope_isolated':True,'channel_only_verified':True,'transaction_rollback':True},ensure_ascii=False))
    def test_enabled_memory_channel_facts_only(self): self.run_case(True)
    def test_disabled_memory_does_not_consume_old_memory(self): self.run_case(False)
    def test_original_openai_serializer_and_full_pipeline(self):
        from application.models import ChatUserType
        from chat.serializers.chat import OpenAIChatSerializer
        from django.core.cache import cache
        from common.constants.cache_version import Cache_Version
        self.addCleanup(lambda:cache.delete(Cache_Version.CHAT.get_key(key=str(self.chat.id)),version=Cache_Version.CHAT_INFO.get_version()))
        seen={}
        with (
            patch('application.chat_pipeline.step.chat_step.impl.base_chat_step.get_model_instance_by_model_workspace_id',return_value=Stub('reply',seen)),
            patch('application.chat_pipeline.step.reset_problem_step.impl.base_reset_problem_step.get_model_instance_by_model_workspace_id',return_value=Stub('optimizer',seen)),
            patch('application.long_term_memory.get_model_instance_by_model_workspace_id',return_value=Stub('memory',seen)),
            patch('chat.serializers.chat.request_memory_config',return_value={'trigger_type':'ROUND','trigger_setting':{'rounds':2},'model_id':str(self.app.model_id),'model_params':{}})
        ):
            response=OpenAIChatSerializer(data={'application_id':str(self.app.id),'chat_user_id':self.scope,
                                               'chat_user_type':ChatUserType.APPLICATION_API_KEY.value,'ip_address':'127.0.0.1',
                                               'source':{'type':'API_CALL','dpin_scope_version':1}}).chat(
                {'chat_id':str(self.chat.id),'stream':False,'messages':[{'role':'user','content':json.dumps(self.input)}]})
        self.assertEqual(response.status_code,200,response.content)
        data=json.loads(response.content); self.assertEqual(data['reply_protocol'],PROTOCOL);self.assertEqual(data['dpin_scope'],self.scope)
        self.assertEqual(str(data['choices'][0]['chat_id']),str(self.chat.id))
        self.assertIn('C2_CONFIRMED_FIXED',seen['reply']);self.assertIn('C2_CHANNEL_CANDIDATE',seen['memory'])
        for text in seen.values():
            for old in ['C2_OLD_UNSENT','C2_OLD_PROBLEM','C2_OLD_MEMORY']:self.assertNotIn(old,text)
        self.assertEqual(ChatRecord.objects.filter(chat=self.chat).count(),2)
        self.assertEqual(ApplicationLongTermMemory.objects.get(application=self.app,chat_user_id=self.scope).memory,'C2_OLD_MEMORY')
        print(json.dumps({'kind':'C2_original_serializer_full_pipeline_real_pg_explicit_model_stub','reply_protocol':data['reply_protocol'],'usage':data['usage'],'record_preserved':True,'memory_scope_isolated':True,'transaction_rollback':True},ensure_ascii=False))

if __name__=='__main__': unittest.main()
