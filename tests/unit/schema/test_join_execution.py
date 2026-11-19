"""Dispatch a node with dependencies and no kind as a join."""

import pytest

from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Node


class TestJoinExecuteDispatch:

    @pytest.mark.asyncio
    async def test_dispatch_returns_empty_success(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(
            id="j", name="J",
            depends_on=["a", "b"],
        )
        result = await executor.execute(node, context={}, workdir=str(tmp_path))
        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_dispatch_ignores_context(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(id="j", name="J")
        result = await executor.execute(
            node,
            context={"a": "upstream-output", "_retry_reason": "irrelevant"},
            workdir=str(tmp_path),
        )
        assert result.output == ""
        assert result.success is True
