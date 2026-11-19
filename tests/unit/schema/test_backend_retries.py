import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Settings


def test_backend_retry_budgets_are_per_kind():
    defaults = Settings().defaults
    assert defaults.agent.backend_max_retries == 2
    assert defaults.llm.backend_max_retries == 2
    assert defaults.run.backend_max_retries == 0


def test_backend_retries_are_independent_of_gate_retries():
    settings = Settings(max_retries=3, defaults={"llm": {"backend_max_retries": 5}})
    assert settings.defaults.llm.backend_max_retries == 5
    assert settings.defaults.agent.backend_max_retries == 2
    assert settings.max_retries == 3


@pytest.mark.parametrize("key", ["backend_max_retries", "backend_max_retry"])
def test_top_level_backend_retry_budget_rejected(key):
    with pytest.raises(ValidationError, match=key):
        Settings.model_validate({key: 5})


def test_negative_backend_retry_budget_rejected():
    with pytest.raises(ValidationError, match="backend_max_retries"):
        Settings(defaults={"agent": {"backend_max_retries": -1}})
