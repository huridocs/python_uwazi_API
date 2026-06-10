import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.python_code_executor import run_python_code


@pytest.fixture
def mock_entity_api():
    api = AsyncMock()
    api.create_entities = AsyncMock(
        return_value=[
            AgentEntityMutationResult(shared_id="abc123", success=True, error=None),
        ]
    )
    api.update_entities = AsyncMock(
        return_value=[
            AgentEntityMutationResult(shared_id="abc123", success=True, error=None),
        ]
    )
    api.delete_entities_by_shared_ids = AsyncMock(
        return_value=[
            AgentEntityMutationResult(shared_id="abc123", success=True, error=None),
        ]
    )
    return api


@pytest.fixture
def mock_deps(mock_entity_api):
    store = EntityStore()
    return UwaziAgentToolsDependencies.model_construct(
        thesauri_api=MagicMock(),
        template_api=MagicMock(),
        template_mapper=MagicMock(),
        entity_api=mock_entity_api,
        page_api=MagicMock(),
        entity_store=store,
    )


class MockRunContext:
    """Minimal mock for pydantic_ai RunContext."""

    def __init__(self, deps):
        self.deps = deps


class TestRunPythonCode:
    @pytest.mark.anyio
    async def test_successful_execution(self, mock_deps):
        code = "result = 'hello world'"
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert output == "hello world"

    @pytest.mark.anyio
    async def test_missing_result_variable(self, mock_deps):
        code = "x = 42"
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert "no 'result' variable was set" in output

    @pytest.mark.anyio
    async def test_entities_available(self, mock_deps):
        entity = AgentEntity(
            shared_id="id1",
            title="Test",
            template_name="T",
            metadata={},
            language="en",
            published=True,
        )
        mock_deps.entity_store.add_entities([entity])
        code = "result = str(len(entities))"
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert output == "1"

    @pytest.mark.anyio
    async def test_create_entities_available(self, mock_deps):
        code = """
result = str(create_entities([{'title': 'New', 'template_name': 'T'}]))
"""
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        mock_deps.entity_api.create_entities.assert_awaited_once()
        assert "abc123" in output

    @pytest.mark.anyio
    async def test_update_entities_available(self, mock_deps):
        entity = AgentEntity(
            shared_id="id1",
            title="Test",
            template_name="T",
            metadata={},
            language="en",
            published=True,
        )
        mock_deps.entity_store.add_entities([entity])
        code = """
result = str(update_entities([{'shared_id': 'id1', 'template_name': 'T', 'title': 'Updated'}]))
"""
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        mock_deps.entity_api.update_entities.assert_awaited_once()
        assert "abc123" in output

    @pytest.mark.anyio
    async def test_delete_entities_available(self, mock_deps):
        code = """
result = str(delete_entities(['id1']))
"""
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        mock_deps.entity_api.delete_entities_by_shared_ids.assert_awaited_once()
        assert "abc123" in output

    @pytest.mark.anyio
    async def test_error_includes_traceback(self, mock_deps):
        code = "result = 1 / 0"
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert "Error executing code" in output
        assert "ZeroDivisionError" in output
        assert "Traceback" in output

    @pytest.mark.anyio
    async def test_standard_libraries_available(self, mock_deps):
        code = """
import math
result = str(math.sqrt(16))
"""
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert output == "4.0"

    @pytest.mark.anyio
    async def test_no_entity_api_error(self, mock_deps):
        mock_deps.entity_api = None
        code = "result = 'ok'"
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert "Error: Entity tools are not configured" in output

    @pytest.mark.anyio
    async def test_multiple_crud_calls_in_single_loop(self, mock_deps):
        """Ensure the single event loop can handle multiple CRUD calls."""
        code = """
result = str(create_entities([{'title': 'A', 'template_name': 'T'}]) +
             create_entities([{'title': 'B', 'template_name': 'T'}]))
"""
        ctx = MockRunContext(mock_deps)
        output = await run_python_code(ctx, code)
        assert mock_deps.entity_api.create_entities.await_count == 2
