import pytest
from backend.scrapers.base import create_agent


def test_create_agent_returns_agent():
    agent = create_agent(
        task="Test task",
        vllm_endpoint="http://localhost:8000/v1",
        vllm_api_key="test-key",
        vllm_model="test-model",
        output_dir="/tmp/test_data",
        headless=True,
    )
    # Agent should be created without errors
    assert agent is not None
    assert agent.task == "Test task"
