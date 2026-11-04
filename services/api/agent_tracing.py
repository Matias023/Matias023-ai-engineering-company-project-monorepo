"""Trazas estructuradas del agente TrackFlow.

No persiste preguntas, respuestas ni documentos.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

TRACE_FILE = (
    Path(__file__).resolve().parent
    / "agent_traces.jsonl"
)


def _write_event(path, event):
    """Escribe un evento JSONL con permisos privados."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(path, flags, 0o600)

    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(event, ensure_ascii=False) + "\n"
            )
    except BaseException:
        raise


def _summarize_node_output(node_name, changes):
    """Metadatos seguros del resultado, sin contenido RAG."""
    if not isinstance(changes, dict):
        return {}

    summary = {}

    hits = changes.get("hits")
    if isinstance(hits, list):
        summary["hit_count"] = len(hits)

    sources = changes.get("sources")
    if isinstance(sources, list):
        summary["source_count"] = len(sources)

    answer = changes.get("answer")
    if isinstance(answer, str):
        summary["answer_length"] = len(answer)

    if isinstance(changes.get("abstained"), bool):
        summary["abstained"] = changes["abstained"]

    if node_name == "receive":
        summary["question_accepted"] = (
            changes.get("reason") != "pregunta_invalida"
        )

    if node_name == "route":
        route = changes.get("route")
        if route in ("rag", "ticket", "ticket_missing_id"):
            summary["source_route"] = route

    if node_name == "ticket_lookup":
        summary["source_route"] = "ticket"

    return summary


def run_traced(
    graph,
    question,
    *,
    thread_id=None,
    trace_file=TRACE_FILE,
    context=None,
):
    """Ejecuta el grafo y registra su recorrido."""
    thread_id = thread_id or str(uuid4())
    run_id = str(uuid4())
    trace_file = Path(trace_file)

    config = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    def record(event_type, **details):
        event = {
            "run_id": run_id,
            "thread_id": thread_id,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat(),
            "event": event_type,
            **details,
        }
        _write_event(trace_file, event)

    record("run_started")

    order = 0

    try:
        stream_kwargs = {
            "config": config,
            "stream_mode": "updates",
        }
        if context is not None:
            stream_kwargs["context"] = context

        for update in graph.stream(
            {"question": question},
            **stream_kwargs,
        ):
            for node_name, changes in update.items():
                order += 1

                reason = (
                    changes.get("reason")
                    if isinstance(changes, dict)
                    else None
                )

                record(
                    "node_completed",
                    node=node_name,
                    order=order,
                    reason=reason,
                    output_summary=_summarize_node_output(
                        node_name, changes
                    ),
                )

        snapshot = graph.get_state(config)
        result = dict(snapshot.values)

        record(
            "run_completed",
            nodes_executed=order,
            abstained=result.get("abstained"),
            reason=result.get("reason"),
        )

        return result

    except Exception:
        # No persistir detalles internos o credenciales.
        record(
            "run_failed",
            nodes_executed=order,
            error="execution_failed",
        )
        raise



def record_guardrail_event(direction, action, category, reason,
                           trace_file=TRACE_FILE):
    """Registra únicamente metadatos permitidos del guardrail."""
    allowed_directions = {"input", "output"}
    allowed_actions = {"reject", "redirect"}
    allowed_categories = {"structural", "content", "security"}
    allowed_reasons = {
        "invalid_input", "jailbreak", "personal_task", "casual",
        "structural", "content", "security",
    }

    if (
        direction not in allowed_directions
        or action not in allowed_actions
        or category not in allowed_categories
        or reason not in allowed_reasons
    ):
        raise ValueError("Evento de guardrail no permitido")

    _write_event(trace_file, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "guardrail_decision",
        "direction": direction,
        "action": action,
        "category": category,
        "reason": reason,
    })


def summarize_guardrail_events(trace_file=TRACE_FILE):
    """Devuelve los conteos agrupados por categoría."""
    counts = {"structural": 0, "content": 0, "security": 0}
    path = Path(trace_file)

    if not path.is_file():
        return counts

    with path.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("event") != "guardrail_decision":
                continue
            category = event.get("category")
            if category in counts:
                counts[category] += 1

    return counts


def read_trace(run_id, trace_file=TRACE_FILE):
    """Consulta únicamente los eventos de una corrida."""
    path = Path(trace_file)

    if not path.is_file():
        return []

    with path.open(encoding="utf-8") as stream:
        return [
            event
            for line in stream
            if (event := json.loads(line)).get("run_id")
            == run_id
        ]


def record_rfp_arbitration(
    ticket_id,
    departments,
    conflicts,
    arbitration_clear,
    *,
    trace_file=None,
):
    """Record a metadata-only RFP arbitration event."""
    from hashlib import sha256

    allowed_triggers = {
        "volume-vs-capacity",
        "returns-sla-breach",
        "currency-mismatch",
    }

    triggers = sorted({
        conflict["trigger_id"]
        for conflict in conflicts
        if isinstance(conflict, dict)
        and conflict.get("trigger_id") in allowed_triggers
    })

    run_id = str(uuid4())

    ticket_reference = sha256(
        ticket_id.encode("utf-8")
    ).hexdigest()[:16]

    event = {
        "run_id": run_id,
        "thread_id": f"rfp:{ticket_reference}",
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),
        "event": "rfp_arbitration_evaluated",
        "department_count": len(departments),
        "trigger_ids": triggers,
        "arbitration_clear": bool(arbitration_clear),
        "decision": (
            "clear"
            if arbitration_clear
            else "request_changes"
        ),
    }

    _write_event(
        TRACE_FILE if trace_file is None else trace_file,
        event,
    )

    return run_id


def record_rfp_review(
    ticket_id,
    department_id,
    result,
    *,
    key_aspect_count,
    had_human_feedback,
    trace_file=None,
):
    """Record safe metadata for a completed RFP review cycle."""
    from hashlib import sha256

    allowed_departments = {"warehouse", "lastmile", "reverse"}
    allowed_evaluators = {"readability", "relevance", "compliance"}
    allowed_decisions = {"evaluation_passed", "needs_human_review"}

    if department_id not in allowed_departments:
        raise ValueError("Invalid department for trace")

    decision = result.get("review_status")

    if decision not in allowed_decisions:
        raise ValueError("Invalid review decision for trace")

    history = result.get("evaluation_results", {}).get("history", [])
    summaries = []

    for entry in history:
        checks = []

        for evaluation in entry.get("evaluations", []):
            name = evaluation.get("evaluator")

            if name not in allowed_evaluators:
                continue

            checks.append({
                "evaluator": name,
                "passed": evaluation.get("passed") is True,
            })

        summaries.append({
            "iteration": entry.get("iteration"),
            "evaluators": checks,
        })

    ticket_reference = sha256(
        ticket_id.encode("utf-8")
    ).hexdigest()[:16]

    run_id = str(uuid4())

    event = {
        "run_id": run_id,
        "thread_id": f"rfp:{ticket_reference}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "rfp_department_review_completed",
        "department": department_id,
        "input_summary": {
            "aspect_count": key_aspect_count,
            "human_feedback_provided": bool(had_human_feedback),
        },
        "iteration_count": result.get("iterations", 0),
        "iteration_summaries": summaries,
        "decision": decision,
        "output_summary": {
            "missing_information_count": len(
                result.get("missing_information", [])
            ),
        },
    }

    _write_event(
        TRACE_FILE if trace_file is None else trace_file,
        event,
    )

    return run_id
