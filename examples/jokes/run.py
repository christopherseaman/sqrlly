"""Run the joke workflow and print detailed output."""
import asyncio
from pathlib import Path

from sqrlly.cli.main import load_config

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state


async def main():
    config = load_config(str(Path(__file__).with_name("workflow.yaml")))
    executor = DispatchExecutor(workdir=".", settings=config.settings)
    compiled = build_workflow_graph(config, executor)
    state = make_initial_state(workflow_name=config.name, workdir=".")
    result = await compiled.ainvoke(state)

    print("=== Workflow Result ===")
    print(f"Completed: {result.get('completed_nodes', set())}")
    print(f"Failed: {result.get('failed_nodes', set())}")

    for err in result.get("errors", []):
        print(f"  Error: {err}")

    scores = {
        node: records[-1].get("result", {}).get("score")
        for node, records in result.get("evaluations", {}).items()
        if records
    }
    print(f"\nGate scores: {scores}")
    print(f"Retries: {result.get('retries', {})}")

    for node_id, output in result.get("node_outputs", {}).items():
        print(f"\n--- {node_id} ---")
        print(output)

    await executor.close()


asyncio.run(main())
