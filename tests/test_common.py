"""Provider protocol and request boundary tests shared by both independent repositories."""
import json

import httpx
import pytest

from app.provider import select_quotes
from app.runtime import BoundedRequests, Security


@pytest.mark.parametrize('body', [
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{bad'}]}]},
    {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text',
        'text': '{"claims":[{"source":[],"quote":"An invalid model output"}]}'}]}]},
    {'status': 'incomplete'},
])
def test_invalid_model_protocol_abstains(body):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    assert select_quotes('question', [], model='mock', api_key='mock', transport=transport) == []


def test_timeout_abstains():
    def timeout(request):
        raise httpx.ReadTimeout('private response content must never escape')
    assert select_quotes('question', [], model='mock', api_key='mock',
                         transport=httpx.MockTransport(timeout)) == []


def test_successful_quote_and_request_contract():
    evidence = [{'id': 'source-1', 'text': 'Approved onboarding policy text.'}]
    claims = [{'source': 'source-1', 'quote': evidence[0]['text']}]
    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == 'https://api.openai.com/v1/responses'
        assert request.headers['Authorization'] == 'Bearer test-key'
        assert payload['store'] is False
        assert payload['text']['format']['strict'] is True
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text',
             'text': json.dumps({'claims': claims})}]}]})
    assert select_quotes('onboarding policy', evidence, model='test-model', api_key='test-key',
                         transport=httpx.MockTransport(handler)) == claims


def test_fabricated_source_abstains():
    def handler(request):
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text',
             'text': json.dumps({'claims': [{'source': 'missing', 'quote': 'Fabricated source text'}]})}]}]})
    assert select_quotes('question', [], model='test', api_key='key',
                         transport=httpx.MockTransport(handler)) == []


@pytest.mark.parametrize('keys', [{}, {'short': 'tenant'}, {'a' * 25: ''}])
def test_unsafe_key_configuration_rejected(keys):
    with pytest.raises(ValueError):
        Security(keys)


def test_chunked_body_limit():
    import asyncio
    messages = [{'type': 'http.request', 'body': b'a' * 4, 'more_body': True},
                {'type': 'http.request', 'body': b'b' * 4, 'more_body': False}]
    sent = []
    async def receive():
        return messages.pop(0)
    async def send(message):
        sent.append(message)
    async def forbidden(*args):
        pytest.fail('oversized request reached application')
    asyncio.run(BoundedRequests(forbidden, limit=7)({'type': 'http'}, receive, send))
    assert sent[0]['status'] == 413
