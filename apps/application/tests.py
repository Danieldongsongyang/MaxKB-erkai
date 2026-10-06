import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage

from application.chat_pipeline.pipeline_manage import PipelineManage
from application.chat_pipeline.step.chat_step.impl.base_chat_step import BaseChatStep
from chat.serializers.chat import ChatSerializers
from common.handle.impl.response.openai_to_response import OpenaiToResponse
from common.handle.impl.response.system_to_response import SystemToResponse


class DPinActualUsageTestCase(SimpleTestCase):
    def test_response_uses_current_invocation_usage_and_never_estimates(self):
        cases = [
            ({"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20}, 200),
            ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 200),
            ({}, 502),
            ({"prompt_tokens": 1}, 502),
            ({"prompt_tokens": True, "completion_tokens": 1}, 502),
            ({"prompt_tokens": -1, "completion_tokens": 1}, 502),
            ({"prompt_tokens": 1.5, "completion_tokens": 1}, 502),
            ({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3}, 502),
            ({"prompt_tokens": 2**31 - 1, "completion_tokens": 1}, 502),
        ]
        for usage, expected_status in cases:
            with self.subTest(usage=usage):
                step = BaseChatStep()
                step.context["start_time"] = 0
                manage = PipelineManage([], OpenaiToResponse(), debug=True)
                manage.context["start_time"] = 0
                message = AIMessage(content="reply", response_metadata={"token_usage": usage})
                model = Mock()
                model.invoke.return_value = message
                model.get_num_tokens.side_effect = AssertionError("must not estimate")
                model.get_num_tokens_from_messages.side_effect = AssertionError("must not estimate")
                handler = Mock()
                with (
                    patch("application.chat_pipeline.step.chat_step.impl.base_chat_step.filter_authorized_ids",
                          return_value=[]),
                    patch("application.chat_pipeline.step.chat_step.impl.base_chat_step.get_runtime_user_id",
                          return_value=None),
                    patch.object(step, "_handle_mcp_request", return_value=None),
                ):
                    response = step.execute_block(
                        [HumanMessage(content="test")], "chat-id", "test", handler,
                        chat_model=model, manage=manage, model_setting={},
                        no_references_setting={"status": "ai_questioning"},
                    )
                self.assertEqual(response.status_code, expected_status)
                data = json.loads(response.content)
                model.invoke.assert_called_once()
                if expected_status == 200:
                    expected_usage = {**usage, "usage_source": "upstream"}
                    self.assertEqual({key: data["usage"][key] for key in expected_usage}, expected_usage)
                    self.assertEqual(data["choices"][0]["message"]["content"], "reply")
                    self.assertEqual(data["choices"][0]["chat_id"], "chat-id")
                    handler.handler.assert_called_once()
                else:
                    self.assertEqual(data["error"]["code"], "MAXKB_ACTUAL_USAGE_UNAVAILABLE")
                    self.assertNotIn("usage", data)
                    self.assertNotIn("choices", data)
                    handler.handler.assert_not_called()

    def test_unverified_response_cannot_be_reported_as_success(self):
        response = OpenaiToResponse().to_block_response("chat", "record", "reply", True, 1, 2)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("usage", json.loads(response.content))

    def test_extra_model_calls_are_rejected_before_generation(self):
        application_id = "01a0dbc2-4722-7681-9fbc-4ddf277cbbd6"
        chat_id = "01a0dbc2-4722-7681-9fbc-4ddf277cbbd7"
        cases = [
            ({"type": "WORK_FLOW"}, False), ({"problem_optimization": True}, False),
            ({"long_term_enable": True}, False), ({"mcp_enable": True}, False),
            ({"tool_enable": True}, False), ({"application_enable": True}, False),
            ({"skill_tool_ids": ["tool-id"]}, False), ({}, True), ({}, False),
        ]
        for changed, active_memory in cases:
            with self.subTest(changed=changed, active_memory=active_memory):
                application = SimpleNamespace(**{
                    "type": "SIMPLE", "problem_optimization": False, "long_term_enable": False,
                    "mcp_enable": False, "tool_enable": False, "application_enable": False,
                    "skill_tool_ids": [], **changed,
                })
                chat_info = SimpleNamespace(
                    application=application, application_id=application_id, chat_user_id="api-user",
                    get_application=Mock(), get_chat_user=Mock(),
                )
                serializer = ChatSerializers(data={
                    "chat_id": chat_id, "application_id": application_id, "chat_user_id": "api-user",
                    "chat_user_type": "API", "debug": True,
                })
                with (
                    patch("chat.serializers.chat.QuerySet") as query,
                    patch.object(serializer, "get_chat_info", return_value=chat_info),
                    patch.object(serializer, "is_valid_application_simple"),
                    patch.object(serializer, "chat_simple", return_value="generated") as generate,
                ):
                    query.return_value.filter.return_value.only.return_value.first.return_value = SimpleNamespace(
                        long_term_enable=active_memory,
                    )
                    response = serializer.chat(
                        {"message": "test", "stream": False, "re_chat": False}, OpenaiToResponse(),
                    )
                if changed or active_memory:
                    generate.assert_not_called()
                    self.assertEqual(response.status_code, 502)
                    self.assertEqual(json.loads(response.content)["error"]["code"],
                                     "MAXKB_ACTUAL_USAGE_UNSUPPORTED")
                else:
                    generate.assert_called_once()
                    self.assertEqual(response, "generated")

    def test_native_maxkb_response_preserves_existing_counting(self):
        step = BaseChatStep()
        step.context["start_time"] = 0
        manage = PipelineManage([], SystemToResponse(), debug=True)
        manage.context["start_time"] = 0
        model = Mock()
        model.get_num_tokens_from_messages.return_value = 5
        model.get_num_tokens.return_value = 2
        with patch.object(step, "get_block_result", return_value=(AIMessage(content="reply"), True)):
            response = step.execute_block(
                [HumanMessage(content="test")], "chat", "test", Mock(),
                chat_model=model, manage=manage, model_setting={},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(manage.context["message_tokens"], 5)
        self.assertEqual(manage.context["answer_tokens"], 2)
        model.get_num_tokens_from_messages.assert_called_once()
        model.get_num_tokens.assert_called_once()
