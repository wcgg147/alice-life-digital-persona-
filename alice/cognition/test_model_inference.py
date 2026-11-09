"""Inference transport contracts; these tests do not assess learning quality."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from .environment import LiveModel


class ModelInferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cfg = {'model': 'LongCat-2.5', 'model_base_url': 'https://api.longcat.chat/openai/v1',
                    'model_api_key': 'test-not-a-secret'}
        self.patch = patch('openai.AsyncOpenAI')
        self.factory = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = self.factory.return_value
        self.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{}'))],
            usage=SimpleNamespace(model_dump=lambda: {'completion_tokens': 2})))
        self.client.chat.completions.create = self.create
        self.client.close = AsyncMock()

    async def test_default_retains_existing_request(self):
        model = LiveModel(self.cfg)
        await model.complete([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(self.create.call_args.kwargs['max_tokens'], 900)
        self.assertEqual(self.create.call_args.kwargs['extra_body'], {'thinking': {'type': 'disabled'}})
        self.assertEqual(self.factory.call_args.kwargs['timeout'], 45)
        self.assertEqual(self.factory.call_args.kwargs['max_retries'], 0)
        self.assertEqual(model.calls, 1)
        await model.close()

    async def test_explicit_reasoning_resources_reach_transport(self):
        cfg = {**self.cfg, 'cognition_inference': {'thinking': 'enabled', 'thinking_budget': 2048,
                                                 'max_tokens': 4096, 'timeout_seconds': 120}}
        model = LiveModel(cfg)
        await model.complete([{'role': 'user', 'content': 'hello'}])
        self.assertEqual(self.create.call_args.kwargs['extra_body'],
                         {'thinking': {'type': 'enabled'}, 'thinking_budget': 2048})
        self.assertEqual(self.create.call_args.kwargs['max_tokens'], 4096)
        self.assertEqual(self.factory.call_args.kwargs['timeout'], 120)
        self.assertEqual(model.last_response['inference']['thinking'], 'enabled')
        self.assertEqual(model.last_response['usage'], {'completion_tokens': 2})
        await model.close()

    async def test_invalid_resources_fail_before_client_creation(self):
        cases = [{'thinking': 'yes'}, {'thinking': []}, {'thinking': 'enabled'}, {'thinking_budget': 100},
                 {'thinking': 'enabled', 'thinking_budget': 900}, {'max_tokens': True},
                 {'max_tokens': 262145}, {'timeout_seconds': float('inf')},
                 {'timeout_seconds': -1}, {'unknown_option': True}]
        for inference in cases:
            with self.subTest(inference=inference), self.assertRaises(ValueError):
                LiveModel({**self.cfg, 'cognition_inference': inference})
        self.factory.assert_not_called()

    async def test_explicit_thinking_is_not_guessed_for_another_model(self):
        with self.assertRaises(ValueError):
            LiveModel({**self.cfg, 'model': 'another-model',
                       'cognition_inference': {'thinking': 'enabled', 'thinking_budget': 400}})
        self.factory.assert_not_called()

    async def test_operator_cancellation_propagates_without_retry(self):
        self.create.side_effect = asyncio.CancelledError
        model = LiveModel(self.cfg)
        with self.assertRaises(asyncio.CancelledError):
            await model.complete([])
        self.assertEqual(model.calls, 1)
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(model.last_response['error_type'], 'CancelledError')
        self.assertIsNone(model.last_response['usage'])
        await model.close()

    async def test_total_deadline_cancels_attempt_without_retry_or_stale_usage(self):
        model = LiveModel({**self.cfg, 'cognition_inference': {'timeout_seconds': .02}})
        await model.complete([])
        cancelled = asyncio.Event()

        async def unfinished(**kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.create.side_effect = unfinished
        with self.assertRaises(TimeoutError):
            await model.complete([])
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(model.calls, 2)
        self.assertIsNone(model.last_response['usage'])
        self.assertEqual(model.last_response['error_type'], 'TimeoutError')
        await model.close()

    async def test_empty_truncated_content_is_observable_without_retry(self):
        self.create.return_value.choices[0].finish_reason = 'length'
        self.create.return_value.choices[0].message.content = ''
        model = LiveModel(self.cfg)
        self.assertEqual(await model.complete([]), '')
        self.assertEqual(model.last_response['finish_reason'], 'length')
        self.assertEqual(self.create.call_count, 1)
        await model.close()

    async def test_opencode_gateway_includes_session_header_and_disabled_thinking(self):
        cfg = {'model': 'deepseek-v4.1-flash', 'model_base_url': 'https://opencode.ai/zen/go/v1',
               'model_api_key': 'test-not-a-secret'}
        model = LiveModel(cfg)
        await model.complete([{'role': 'user', 'content': 'hello'}])
        headers = self.factory.call_args.kwargs.get('default_headers') or {}
        self.assertTrue(str(headers.get('x-opencode-session', '')).startswith('ses_'))
        self.assertEqual(self.create.call_args.kwargs['extra_body'], {'thinking': {'type': 'disabled'}})
        await model.close()


class ModelSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_sends_thinking_budget_in_actual_json_body(self):
        import json
        import httpx
        from openai import AsyncOpenAI
        requests = []

        async def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': 'LongCat-2.5',
                'choices': [{'index': 0, 'finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': '{}'}}]})

        def client(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

        cfg = {'model': 'LongCat-2.5', 'model_base_url': 'https://example.invalid/v1',
               'model_api_key': 'test-only', 'cognition_inference': {
                   'thinking': 'enabled', 'thinking_budget': 2048, 'max_tokens': 4096}}
        with patch('openai.AsyncOpenAI', side_effect=client):
            model = LiveModel(cfg)
            try:
                await model.complete([{'role': 'user', 'content': 'fixture'}])
            finally:
                await model.close()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['thinking'], {'type': 'enabled'})
        self.assertEqual(requests[0]['thinking_budget'], 2048)
        self.assertEqual(requests[0]['max_tokens'], 4096)


if __name__ == '__main__':
    unittest.main()
