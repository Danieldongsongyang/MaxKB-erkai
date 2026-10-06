# coding=utf-8
"""实际用量的隔离契约测试，不连接数据库或模型服务。"""
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps'))
from common.handle.dpin_actual_usage import MAX_TOKENS, ActualUsageUnavailable, RequestActualUsage, actual_token_usage


def message(prompt=2, completion=3, response_id=None):
    return SimpleNamespace(id=response_id,
                           usage_metadata={'input_tokens': prompt, 'output_tokens': completion,
                                           'total_tokens': prompt + completion}, response_metadata={})


class ActualUsageTests(unittest.TestCase):
    def test_sources_agree_and_zero_is_actual(self):
        value = message(0, 0)
        value.response_metadata = {'token_usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}
        self.assertEqual(actual_token_usage(value), (0, 0))

    def test_invalid_quantities_and_sources(self):
        bad = [None, {}, {'input_tokens': 1}, {'input_tokens': True, 'output_tokens': 0},
               {'input_tokens': -1, 'output_tokens': 0}, {'input_tokens': 1.0, 'output_tokens': 0},
               {'input_tokens': '1', 'output_tokens': 0}, {'input_tokens': MAX_TOKENS, 'output_tokens': 1},
               {'input_tokens': 1, 'output_tokens': 0, 'total_tokens': 2},
               {'input_tokens': 1, 'output_tokens': 0, 'prompt_tokens': 2, 'completion_tokens': 0}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ActualUsageUnavailable):
                actual_token_usage(SimpleNamespace(usage_metadata=value, response_metadata={}))
        for other in [None, {}, [], {'prompt_tokens': 9, 'completion_tokens': 1}]:
            value = message()
            value.response_metadata = {'token_usage': other}
            with self.subTest(other=other), self.assertRaises(ActualUsageUnavailable):
                actual_token_usage(value)

    def test_multiple_calls_sum_and_seal(self):
        usage = RequestActualUsage()
        for purpose, prompt, completion in [('problem_optimization', 1, 2), ('reply', 3, 4),
                                            ('long_term_memory', 5, 6)]:
            usage.complete(usage.begin(purpose), message(prompt, completion))
        self.assertEqual(usage.finalize(), (9, 12))
        self.assertEqual(len(usage.details()), 3)
        self.assertEqual(usage.finalize(), (9, 12))
        with self.assertRaises(ActualUsageUnavailable):
            usage.begin('reply')

    def test_empty_pending_failure_and_missing_poison_request(self):
        for action in ['empty', 'pending', 'failed', 'missing']:
            usage = RequestActualUsage()
            if action != 'empty':
                call = usage.begin('reply')
                if action == 'failed':
                    usage.fail(call)
                if action == 'missing':
                    with self.assertRaises(ActualUsageUnavailable):
                        usage.complete(call, SimpleNamespace())
            with self.subTest(action=action), self.assertRaises(ActualUsageUnavailable):
                usage.finalize()

    def test_duplicate_ticket_response_and_aggregate_overflow(self):
        for action in ['ticket', 'response', 'overflow']:
            usage = RequestActualUsage()
            call = usage.begin('reply')
            usage.complete(call, message(MAX_TOKENS if action == 'overflow' else 2, 0, 'upstream-1'))
            with self.subTest(action=action), self.assertRaises(ActualUsageUnavailable):
                usage.complete(call if action == 'ticket' else usage.begin('long_term_memory'),
                               message(1, 0, 'upstream-1' if action == 'response' else 'upstream-2'))
            with self.assertRaises(ActualUsageUnavailable):
                usage.finalize()

    def test_concurrent_requests_share_model_but_not_usage(self):
        # 每个请求可收到相同上游 ID；去重范围仅限本次请求。
        def request(index):
            usage = RequestActualUsage()
            usage.complete(usage.begin('reply'), message(index, 1, 'reply'))
            usage.complete(usage.begin('long_term_memory'), message(2, index, 'memory'))
            return usage.finalize()
        with ThreadPoolExecutor(max_workers=8) as executor:
            actual = list(executor.map(request, range(64)))
        self.assertEqual(actual, [(i + 2, i + 1) for i in range(64)])

    def test_concurrent_completion_inside_one_request(self):
        usage = RequestActualUsage()
        calls = [usage.begin('reply') for _ in range(32)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(lambda item: usage.complete(item[1], message(item[0], 1)), enumerate(calls)))
        self.assertEqual(usage.finalize(), (sum(range(32)), 32))


if __name__ == '__main__':
    unittest.main()
