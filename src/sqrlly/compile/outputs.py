"""Check the output of each successful execution before the node settles."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from sqrlly.runtime.gates import validate_output_contract
from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Outputs


async def execute_with_outputs(
    execute: Callable[[dict[str, Any]], Awaitable[ExecutionResult]],
    context: dict[str, Any],
    outputs: Outputs | None,
    workdir: str,
) -> tuple[ExecutionResult, int]:
    """Return the final result and number of node bodies executed."""
    if outputs and outputs.schema_file and outputs.schema_document is None:
        try:
            outputs.load_schema(Path(workdir) / outputs.schema_file)
        except ValueError as exc:
            return ExecutionResult(success=False, error=str(exc), error_kind="node_error"), 0
    retained_refs = set()
    for attempt in range(1, 4):
        result = await execute(context)
        retained_refs.update(result.retained_refs)
        result.retained_refs = retained_refs.copy()
        if not result.success or outputs is None:
            return result, attempt
        missing = validate_output_contract(outputs, result.worktree or workdir)
        errors = ["missing files: " + ", ".join(missing)] if missing else []
        if outputs.schema_file:
            try:
                value = result.structured_output
                if value is None:
                    value = json.loads(result.output)
                outputs.validate_value(value)
                result.structured_output = value
            except ValidationError as exc:
                errors.append(f"outputs.schema {exc.json_path}: {exc.message}")
            except ValueError as exc:
                errors.append(f"outputs.schema: {exc}")
        if not errors:
            return result, attempt
        reason = "Output contract violated: " + "; ".join(errors)
        if attempt == 3:
            result.success = False
            result.error = reason
            result.error_kind = "node_error"
            return result, attempt
        context = {**context, "_output_retry_reason": reason, "_retry_reason": reason}
