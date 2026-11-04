"""Endpoint autenticado del agente LangGraph TrackFlow."""

from fastapi import APIRouter, Depends, HTTPException, Response

from routers.rag import RAGAnswer, RAGQuestion
from security import get_current_user, oauth2_scheme
from models import UserResponse
from agent_memory import AgentMemory
from agent_guardrails import classify_input, validate_output
from agent_tracing import record_guardrail_event
from pydantic import BaseModel, Field

router = APIRouter(
    prefix="/agent",
    tags=["Agent"],
    dependencies=[Depends(get_current_user)],
)


def get_agent_runner():
    """Proporciona el ejecutor; reemplazable en tests."""
    from agent_persistence import persistent_agent
    from agent_tracing import run_traced

    def execute(
        question: str, *, token: str, approved_memories=None
    ):
        context = {
            "token": token,
            "approved_memories": approved_memories or [],
        }
        with persistent_agent() as graph:
            return run_traced(graph, question, context=context)

    return execute


def get_memory_store():
    """Almacenamiento reemplazable en pruebas."""
    return AgentMemory()


class AgentAnswer(RAGAnswer):
    memory_proposal: dict | None = None


@router.post("/query", response_model=AgentAnswer)
def query_agent(
    payload: RAGQuestion,
    response: Response,
    runner=Depends(get_agent_runner),
    current_user: UserResponse = Depends(get_current_user),
    token: str = Depends(oauth2_scheme),
    store: AgentMemory = Depends(get_memory_store),
):
    question = payload.question.strip()

    if not question:
        raise HTTPException(
            status_code=422,
            detail="La pregunta no puede estar vacía",
        )

    decision = classify_input(question)
    if decision.action != "allow":
        category = (
            "security" if decision.reason == "jailbreak"
            else "structural" if decision.reason == "invalid_input"
            else "content"
        )
        record_guardrail_event(
            "input", decision.action, category, decision.reason
        )
        return {
            "answer": decision.response or "Consulta no permitida.",
            "abstained": True,
            "reason": f"guardrail_{decision.reason}",
            "sources": [],
            "threshold": 0.0,
            "memory_proposal": None,
        }

    try:
        try:
            approved_memories = [
                row["content"]
                for row in store.list_memories(current_user.id)
            ]
        except (OSError, ValueError):
            approved_memories = []

        result = runner(
            question,
            token=token,
            approved_memories=approved_memories,
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="El agente no está disponible",
        ) from None

    output_decision = validate_output(result)
    if output_decision.action != "allow":
        record_guardrail_event(
            "output", "reject",
            output_decision.reason, output_decision.reason,
        )
        return {
            "answer": "No puedo entregar esta respuesta de forma segura.",
            "abstained": True,
            "reason": f"guardrail_{output_decision.reason}",
            "sources": [],
            "threshold": 0.0,
            "memory_proposal": None,
        }

    from agent_memory_evaluator import evaluate_memory_candidate

    candidate = (
        evaluate_memory_candidate(question)
        if result.get("abstained") is False
        else None
    )

    if candidate is not None:
        try:
            if store.pending(current_user.id) is None:
                proposal_id = store.propose(
                    current_user.id,
                    candidate["content"],
                )
                response.headers["X-Agent-Memory-Proposal"] = str(
                    proposal_id
                )
                response.headers["X-Agent-Memory-Consent"] = (
                    "Hay una preferencia que podria recordar. "
                    "Revisa la propuesta pendiente y confirma "
                    "si deseas guardarla."
                )
        except (ValueError, OSError):
            pass

    try:
        pending = store.pending(current_user.id)
    except (OSError, ValueError):
        pending = None

    if pending is not None:
        result = {
            **result,
            "memory_proposal": {
                "id": pending["id"],
                "content": pending["content"],
                "question": (
                    "¿Quieres que recuerde esta preferencia?"
                ),
            },
        }

    return result


# ===== AGENT MEMORY API =====



class MemoryProposalInput(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class MemoryDecisionInput(BaseModel):
    approved: bool
    edited_content: str | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
    )


@router.get("/memory")
def list_agent_memories(
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    return {
        "memories": store.list_memories(current_user.id)
    }


@router.get("/memory/pending")
def get_pending_memory(
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    return {
        "proposal": store.pending(current_user.id)
    }


@router.post("/memory/proposals", status_code=201)
def create_memory_proposal(
    payload: MemoryProposalInput,
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    try:
        proposal_id = store.propose(
            current_user.id,
            payload.content,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from None

    return {
        "proposal_id": proposal_id,
        "status": "pending",
    }


@router.post("/memory/proposals/{proposal_id}/decision")
def decide_memory_proposal(
    proposal_id: int,
    payload: MemoryDecisionInput,
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    if not payload.approved and payload.edited_content is not None:
        raise HTTPException(
            status_code=422,
            detail="Cannot edit a rejected proposal",
        )

    try:
        decision = store.decide(
            current_user.id,
            proposal_id,
            payload.approved,
            edited_content=payload.edited_content,
        )
    except ValueError as exc:
        if str(exc) == "Pending proposal not found":
            raise HTTPException(
                status_code=404,
                detail="Pending proposal not found",
            ) from None

        raise HTTPException(
            status_code=422,
            detail="Invalid memory decision",
        ) from None

    return {"status": decision}


@router.get("/memory/history")
def get_memory_history(
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    return {
        "history": store.list_proposals(current_user.id)
    }


@router.delete("/memory/{memory_id}")
def delete_agent_memory(
    memory_id: int,
    current_user: UserResponse = Depends(get_current_user),
    store: AgentMemory = Depends(get_memory_store),
):
    deleted = store.delete_memory(
        current_user.id,
        memory_id,
    )
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Memory not found",
        )
    return {"deleted": True}
