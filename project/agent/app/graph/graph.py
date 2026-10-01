"""LangGraph wiring: every incident starts with the same deterministic
evidence gathering, then Supervisor routes it down one of two paths:

    investigate -> assess_severity -> historical -> supervisor
    supervisor  -> auto_plan   (low-severity, high-confidence historical match)
    supervisor  -> rca         (everything else)
    supervisor  -> investigate (agentic mode: gather_more, capped by agent_max_steps)
    supervisor  -> escalate    (agentic mode: hand straight to a human)
    rca         -> escalate    (low_confidence diagnosis)
    rca         -> plan        (otherwise)
    auto_plan / plan -> reflect    (agentic self-critique; may loop to investigate)
    reflect     -> guardrail   (deterministic plan sanity check, defense in depth)
    guardrail   -> escalate    (guardrail rejects the plan)
    guardrail   -> END         (plan is fit to show a human)   <- HITL gate
    escalate    -> END                                         <- HITL gate

HITL (human-in-the-loop) is NOT a graph node - the graph's only job is to
reach END with either a pending_approval plan or an escalation_reason.
Remediation only runs after a human approves via ui/routes.py; the graph
itself never resumes (see executor.py, unchanged).
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes.auto_plan import auto_plan
from .nodes.escalate import escalate
from .nodes.guardrail import guardrail_node, route_after_guardrail
from .nodes.historical import historical
from .nodes.investigate import investigate
from .nodes.plan import plan
from .nodes.rca import rca, route_after_rca
from .nodes.reflect import reflect, route_after_reflect
from .nodes.severity import severity
from .nodes.supervisor import route_after_supervisor, supervisor
from .state import IncidentState

# Node ids as constants: avoids repeating/mistyping the same string across
# add_node/add_edge calls. ASSESS_SEVERITY can't be named "severity" - that
# collides with the IncidentState "severity" key.
INVESTIGATE = "investigate"
ASSESS_SEVERITY = "assess_severity"
HISTORICAL = "historical"
SUPERVISOR = "supervisor"
AUTO_PLAN = "auto_plan"
RCA = "rca"
PLAN = "plan"
REFLECT = "reflect"
GUARDRAIL = "guardrail"
ESCALATE = "escalate"


def build_graph():
    builder = StateGraph(IncidentState)
    builder.add_node(INVESTIGATE, investigate)
    builder.add_node(ASSESS_SEVERITY, severity)
    builder.add_node(HISTORICAL, historical)
    builder.add_node(SUPERVISOR, supervisor)
    builder.add_node(AUTO_PLAN, auto_plan)
    builder.add_node(RCA, rca)
    builder.add_node(PLAN, plan)
    builder.add_node(REFLECT, reflect)
    builder.add_node(GUARDRAIL, guardrail_node)
    builder.add_node(ESCALATE, escalate)

    # Deterministic evidence-gathering chain - identical for every incident.
    builder.add_edge(START, INVESTIGATE)
    builder.add_edge(INVESTIGATE, ASSESS_SEVERITY)
    builder.add_edge(ASSESS_SEVERITY, HISTORICAL)
    builder.add_edge(HISTORICAL, SUPERVISOR)

    # Supervisor: replay a trusted historical plan, run full RCA, gather more
    # evidence (agentic mode only - loops back to investigate, hard-capped by
    # agent_max_steps), or escalate straight to a human.
    builder.add_conditional_edges(
        SUPERVISOR,
        route_after_supervisor,
        {AUTO_PLAN: AUTO_PLAN, RCA: RCA, INVESTIGATE: INVESTIGATE, ESCALATE: ESCALATE},
    )
    # RCA: hand off to a human when confidence is too low to plan against.
    builder.add_conditional_edges(RCA, route_after_rca, {ESCALATE: ESCALATE, PLAN: PLAN})

    # Both planners converge on a self-critique (agentic mode) that can loop
    # back to investigate for more evidence, then the deterministic guardrail.
    builder.add_edge(AUTO_PLAN, REFLECT)
    builder.add_edge(PLAN, REFLECT)
    builder.add_conditional_edges(
        REFLECT, route_after_reflect, {INVESTIGATE: INVESTIGATE, GUARDRAIL: GUARDRAIL}
    )
    # END here = pending_approval, waiting on the HITL gate in ui/routes.py.
    builder.add_conditional_edges(GUARDRAIL, route_after_guardrail, {ESCALATE: ESCALATE, END: END})
    builder.add_edge(ESCALATE, END)  # END here = escalated, also waiting on a human

    return builder.compile()


graph = build_graph()
