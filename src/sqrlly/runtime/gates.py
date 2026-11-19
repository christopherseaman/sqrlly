from __future__ import annotations

import glob
import json
from pathlib import Path

from sqrlly.runtime._json import extract_json
from sqrlly.runtime.executor.prompt import render_template
from sqrlly.runtime.result import EvaluationBackendError, EvaluationError
from sqrlly.schema.models import Node, OutputContract


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
    if measurement.run:
        run = measurement.run
        argv = [render_template(arg, context) for arg in run.cmd]
        env = {k: render_template(v, context) for k, v in run.env.items()}
        timeout = measurement.timeout or settings.defaults.run.timeout or settings.default_timeout
        result = await run_command(argv, env, workdir, timeout)
        if result.exit_code is None:
            raise EvaluationBackendError(result.error, result.error_kind or "node_error")
        try:
            data = json.loads(result.output)
        except ValueError:
            data = {}
        return {**(data if isinstance(data, dict) else {}), "text": result.output,
                "exit_code": result.exit_code}

    kind = "agent" if measurement.agent else "llm"
    prompt = getattr(measurement, kind)
    body = prompt.text if prompt.text is not None else (directory / settings.prompts / prompt.prompt).read_text()
    schema_file = measurement.outputs and measurement.outputs.schema_file
    schema = json.loads((directory / schema_file).read_text()) if schema_file else {
        "type": "object", "properties": {"score": {"type": "number"}, "explanation": {"type": "string"}},
        "required": ["score", "explanation"],
    }
    prompt = prompt.model_copy(update={"prompt": None, "text": body + "\nReturn JSON matching this schema: {{ _eval_schema }}",
                                       "model": measurement.model or prompt.model})
    judge = Node(id=node.id, worktree="off", timeout=measurement.timeout, **{kind: prompt})
    dispatch = executor or DispatchExecutor(workdir, settings)
    result = await dispatch.execute(judge, {**context, "_eval_schema": json.dumps(schema)},
                                    workdir=workdir, settings_override=settings)
    if not result.success:
        raise EvaluationBackendError(result.error, result.error_kind or "backend_error")
    fields = json.loads(extract_json(result.output, objects_only=True))
    if not isinstance(fields, dict):
        raise EvaluationError("eval requires a JSON object")
    if not schema_file and (type(fields.get("score")) not in (int, float)
                            or not isinstance(fields.get("explanation"), str)):
        raise EvaluationError("eval requires a numeric score and an explanation string")
    return {**fields, "text": result.output}


def scaffold_output_directory(contract: OutputContract, workdir: str) -> None:
    """Pre-create the output directory tree for a node's output contract."""
    base = Path(workdir) / contract.base_directory
    base.mkdir(parents=True, exist_ok=True)


def validate_output_contract(
    contract: OutputContract,
    workdir: str,
) -> list[str]:
    """Check each required entry as a glob that needs at least one match
    (a literal path matches itself). Returns the unmatched entries
    (workdir-relative, ``base_directory`` prepended)."""
    # Follows the promote filter's ``:(glob)`` pathspec: the literal path is
    # tried first (so ``x[1].txt`` matches a file of that name), then ``**``
    # spans directories and ``*`` stays in one segment and matches dotfiles.
    # Glob matches under ``.git`` or ``.sqrlly`` (other nodes' worktrees) are
    # ignored: promote never carries them, so they cannot satisfy a contract.
    missing = []
    for rel in contract.required_paths():
        if not ((Path(workdir) / rel).exists() or any(
                not {".git", ".sqrlly"} & set(Path(match).parts)
                for match in glob.iglob(
                    rel, root_dir=workdir, recursive=True, include_hidden=True))):
            missing.append(rel)
    return missing
