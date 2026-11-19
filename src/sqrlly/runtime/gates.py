from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path

from jsonschema import ValidationError

from sqrlly.runtime.executor.prompt import render_template
from sqrlly.runtime.result import EvaluationBackendError, EvaluationError
from sqrlly.schema.models import Node, Outputs


def build_eval_preamble(context, history, attempt, variables):
    if context == "none":
        return ""
    if context in ("all", "last"):
        records = history if context == "all" else history[-1:]
        return "\n".join(
            f"Attempt {r['attempt']}: {json.dumps(r['result'], ensure_ascii=False)}"
            for r in records
        )
    return render_template(context, {**variables, "attempt": attempt, "history": history,
                                     "eval": history[-1]["result"] if history else {}})


async def run_evaluation(node, context, workdir, settings, executor):
    from sqrlly.runtime.executor.dispatch import DispatchExecutor, run_command

    measurement = node.eval
    directory = settings.workflow_dir or Path(workdir)
    context = {"workflow_dir": str(directory.resolve()), **context}
    outputs = measurement.outputs
    if outputs and outputs.schema_file and outputs.schema_document is None:
        outputs.load_schema(directory / outputs.schema_file)
    if measurement.run:
        run = measurement.run
        argv = [render_template(arg, context) for arg in run.cmd]
        env = {k: render_template(v, context) for k, v in run.env.items()}
        timeout = measurement.timeout or settings.defaults.run.timeout or settings.default_timeout
        result = await run_command(argv, env, workdir, timeout)
        if result.exit_code is None:
            raise EvaluationBackendError(result.error, result.error_kind or "node_error")
        if outputs and outputs.schema_file:
            data = _validated_eval_output(outputs, result)
        else:
            try:
                data = json.loads(result.output)
            except ValueError:
                data = {}
        return {**(data if isinstance(data, dict) else {}), "text": result.output,
                "exit_code": result.exit_code}

    kind = "agent" if measurement.agent else "llm"
    prompt = getattr(measurement, kind)
    if measurement.model:
        prompt = prompt.model_copy(update={"model": measurement.model})
    if not outputs or not outputs.schema_file:
        schema_path = Path(__file__).with_name("eval-score.schema.json")
        outputs = Outputs(schema=str(schema_path))
        outputs.load_schema(schema_path)
    judge = Node(id=node.id, timeout=measurement.timeout, outputs=outputs,
                 **({"worktree": "off"} if measurement.agent else {}), **{kind: prompt})
    dispatch = executor or DispatchExecutor(workdir, settings)
    result = await dispatch.execute(judge, context, workdir=workdir, settings_override=settings)
    if not result.success:
        raise EvaluationBackendError(result.error, result.error_kind or "backend_error")
    fields = _validated_eval_output(outputs, result)
    return {**(fields if isinstance(fields, dict) else {}), "text": result.output}


def _validated_eval_output(outputs, result):
    try:
        value = result.structured_output
        if value is None:
            value = json.loads(result.output)
        outputs.validate_value(value)
    except (ValueError, ValidationError) as exc:
        raise EvaluationError(f"eval.outputs.schema {Path(outputs.schema_file).name}: {exc}") from exc
    return value


def matching_output_files(pattern: str, workdir: str):
    root = Path(workdir).resolve()
    internal = {".git", ".sqrlly"}

    def matches(directory, parts):
        part, *rest = parts
        recursive = part == "**"
        if recursive and rest:
            yield from matches(directory, rest)
        if recursive or any(c in part for c in "*?["):
            candidates = directory.iterdir()
        else:
            candidates = [directory / part]
        for candidate in candidates:
            if candidate.name in internal or candidate.is_symlink():
                continue
            if not recursive and not fnmatchcase(candidate.name, part):
                continue
            if not rest and candidate.is_file():
                yield candidate
            if candidate.is_dir() and (recursive or rest):
                yield from matches(candidate, parts if recursive else rest)

    parts = Path(pattern).parts
    if Path(pattern).is_absolute() or ".." in parts or internal.intersection(parts):
        return
    literal = root / pattern
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            break
    else:
        if literal.is_file():
            yield literal
    if parts:
        for match in matches(root, parts):
            if match != literal:
                yield match


def validate_output_contract(contract: Outputs, workdir: str) -> list[str]:
    """Return each declared file pattern with no match in the execution tree."""
    return [pattern for pattern in contract.files if not next(matching_output_files(pattern, workdir), None)]
