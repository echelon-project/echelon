import pytest

from echelon_engine.agent import partner
from echelon_engine.atoms import routing


def test_named_model_resolution_failure_never_selects_default(monkeypatch, tmp_path):
    def fail(model):
        raise ValueError('api_key=private-value')

    def forbidden():
        pytest.fail('explicit model failure selected a different provider')

    monkeypatch.setattr(routing, 'provider_for', fail)
    monkeypatch.setattr(partner, '_default_provider_model', forbidden)
    with pytest.raises(RuntimeError, match='no fallback provider') as caught:
        partner.dispatch('inspect', scope='fixture', folder=str(tmp_path), model='named-model')
    assert 'private-value' not in str(caught.value)
