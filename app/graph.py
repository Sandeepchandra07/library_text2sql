from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes.output import generate_answer, generate_chart, update_memory
from .nodes.preprocess import correct_asr_errors, resolve_followup
from .nodes.sql import execute_sql, generate_sql, retry_generate, route_after_execute
from .nodes.understand import question_understanding
from .state import GraphState


def _after_understanding(state: GraphState) -> str:
    return "generate_sql" if state.get("route") == "sql" else "update_memory"


def build_graph():
    g = StateGraph(GraphState)
    for name, fn in [
        ("correct_asr_errors", correct_asr_errors), ("resolve_followup", resolve_followup),
        ("question_understanding", question_understanding), ("generate_sql", generate_sql),
        ("execute_sql", execute_sql), ("retry_generate", retry_generate), ("generate_chart", generate_chart),
        ("generate_answer", generate_answer), ("update_memory", update_memory),
    ]:
        g.add_node(name, fn)

    g.set_entry_point("correct_asr_errors")
    g.add_edge("correct_asr_errors", "resolve_followup")
    g.add_edge("resolve_followup", "question_understanding")
    g.add_conditional_edges("question_understanding", _after_understanding,
                            {"generate_sql": "generate_sql", "update_memory": "update_memory"})
    g.add_edge("generate_sql", "execute_sql")
    g.add_conditional_edges("execute_sql", route_after_execute,
                            {"retry_generate": "retry_generate", "generate_chart": "generate_chart", "END": END})
    g.add_edge("retry_generate", "execute_sql")
    g.add_edge("generate_chart", "generate_answer")
    g.add_edge("generate_answer", "update_memory")
    g.add_edge("update_memory", END)
    return g.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def initial_state(question: str, thread_id: str, user_id: str, role: str) -> GraphState:
    return {"raw_question": question, "thread_id": thread_id, "user_id": user_id, "role": role,
            "attempts": 0, "timings": {}}
