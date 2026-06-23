from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict, cast

from langgraph.graph import END, START, StateGraph

ProgressCallback = Callable[[str], None]
RouteLiteral = Literal["finish", "validate", "execute", "terminated", "loop", "end"]


class AgentGraphState(TypedDict, total=False):
    step_id: int
    action_id: str
    decision_kind: str
    decision: Any
    finish_claim: str
    terminated: bool
    route: RouteLiteral
    validation_error: dict[str, str] | None
    result: dict[str, Any] | None
    artifact_refs: list[str]
    command_result_id: str


GraphNode = Callable[[AgentGraphState], AgentGraphState]
GraphRouter = Callable[[AgentGraphState], RouteLiteral]


def compile_linear_agent_graph(
    *,
    plan_node: GraphNode,
    validate_node: GraphNode,
    execute_node: GraphNode,
    finish_node: GraphNode,
    route_after_plan: GraphRouter,
    route_after_validate: GraphRouter,
    route_after_execute: GraphRouter,
):
    graph = StateGraph(AgentGraphState)
    graph.add_node("plan", cast(Any, plan_node))
    graph.add_node("validate", cast(Any, validate_node))
    graph.add_node("execute", cast(Any, execute_node))
    graph.add_node("finish", cast(Any, finish_node))
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan",
        route_after_plan,
        {
            "finish": "finish",
            "validate": "validate",
            "terminated": END,
        },
    )
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "execute": "execute",
            "loop": "plan",
            "terminated": END,
        },
    )
    graph.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "loop": "plan",
            "terminated": END,
        },
    )
    graph.add_edge("finish", END)
    return graph.compile()
