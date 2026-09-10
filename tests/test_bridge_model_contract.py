import json
import pytest

from echelon_engine.atoms.providers.bridge import BridgeProvider


def test_retired_repository_is_not_imported_or_called():
    provider = BridgeProvider(timeout=2)
    response = provider.send([{'role': 'user', 'content': 'must not dispatch'}], 'requested-model')
    assert response.status == 'error'
    assert response.raw['error_code'] == 'retired_bridge'
    assert provider._client is None


class Client:
    END_MARKER = '<end>'
    MARKER_INSTRUCTION = '\nend with <end>'

    def __init__(self, models):
        self.models = models
        self.calls = []

    def send_structured_request(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if prompt == '__list_models__':
            return {'models': self.models, 'protocol_versions': [2]}
        return {'protocol_version': 2, 'requested_model': kwargs['model_id'],
                'selected_model': {'id': kwargs['model_id']}, 'status': 'success',
                'complete': True, 'content': 'review response', 'request_id': 'fixture'}


def run(client, model):
    provider = BridgeProvider(timeout=2)
    provider._client = client
    return provider.send([{'role': 'user', 'content': 'private task brief'}], model)


def test_missing_model_cannot_fall_back_to_first_catalog_entry():
    client = Client([{'id': 'gpt-4o-mini', 'name': 'GPT-4o mini'},
                     {'id': 'auto', 'name': 'Auto', 'family': 'claude-fable-5.1'}])
    result = run(client, 'claude-fable-5-1')
    assert result.status == 'error'
    assert len(client.calls) == 1
    assert client.calls[0][0] == '__list_models__'
    assert result.raw['model_identity_status'] == 'unavailable'


def test_exact_id_is_requested_despite_substring_neighbor():
    client = Client([{'id': 'target-preview', 'name': 'Preview'}, {'id': 'target', 'name': 'Target'}])
    result = run(client, 'target')
    assert result.status == 'success'
    assert client.calls[1][1]['model_id'] == 'target'


def test_structured_response_carries_server_confirmed_model_identity():
    client = Client([{'id': 'target', 'name': 'Target'}])
    result = run(client, 'target')
    assert result.status == 'success'
    assert len(client.calls) == 2
    assert client.calls[1][1]['model_id'] == 'target'
    assert result.raw['model_identity_status'] == 'server_confirmed'
    assert result.raw['usage_basis'] == 'character_estimate'


def test_catalog_failure_never_sends_task_or_exposes_exception_content():
    client = Client([])

    def fail(prompt, **kwargs):
        client.calls.append(prompt)
        raise OSError('secret credential')

    client.send_structured_request = fail
    result = run(client, 'target')
    assert result.status == 'error'
    assert client.calls == ['__list_models__']
    assert 'secret credential' not in result.content


def test_legacy_server_receives_no_task_prompt():
    client = Client([{'id': 'target'}])
    def legacy(prompt, **kwargs):
        client.calls.append(prompt)
        return {'models': client.models}
    client.send_structured_request = legacy
    result = run(client, 'target')
    assert result.status == 'error'
    assert result.raw['model_identity_status'] == 'protocol_unavailable'
    assert client.calls == ['__list_models__']


@pytest.mark.parametrize('changed', [
    {'selected_model': {'id': 'wrong'}}, {'requested_model': 'wrong'},
    {'complete': False}, {'status': 'error'}, {'protocol_version': 1},
])
def test_mismatched_or_incomplete_response_cannot_succeed(changed):
    client = Client([{'id': 'target'}])
    original = client.send_structured_request
    def corrupt(prompt, **kwargs):
        result = original(prompt, **kwargs)
        if prompt != '__list_models__':
            result.update(changed)
        return result
    client.send_structured_request = corrupt
    assert run(client, 'target').status == 'error'
