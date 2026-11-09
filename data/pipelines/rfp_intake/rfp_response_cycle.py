"""Bounded generator-evaluator cycle for TrackFlow RFP proposals."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .rfp_response_generator import generate_department_proposal
from .rfp_response_evaluators import evaluate_proposal, EvaluationResult
from .rfp_compliance import check_commercial_rules, check_client_identity

EVALUATORS = ("readability", "relevance", "compliance")


def run_department_review(
    department_id: str,
    key_aspects: list[str],
    metadata: dict[str, Any],
    *,
    max_iterations: int = 3,
    initial_feedback: str | None = None,
    generator: Callable = generate_department_proposal,
    evaluator: Callable = evaluate_proposal,
) -> dict[str, Any]:
    """Generate and evaluate one department without human approval."""

    if not isinstance(max_iterations, int) or isinstance(
        max_iterations, bool
    ) or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")

    if department_id not in {"warehouse", "lastmile", "reverse"}:
        raise ValueError("Unknown department")

    if initial_feedback is not None and (
        not isinstance(initial_feedback, str)
        or not initial_feedback.strip()
    ):
        raise ValueError("initial_feedback must be nonempty text")

    feedback = (
        initial_feedback.strip()
        if initial_feedback is not None
        else None
    )
    history = []

    for iteration in range(1, max_iterations + 1):
        generated = generator(
            department_id,
            key_aspects,
            metadata,
            feedback="\n".join(dict.fromkeys(
                part for part in (
                    initial_feedback.strip() if initial_feedback else None,
                    feedback,
                ) if part
            )) or None,
        )

        draft = generated["draft_content"]

        def run_evaluator(name: str):
            result = evaluator(
                name,
                department_id,
                draft,
                key_aspects,
                metadata,
            )
            if result.evaluator != name:
                raise ValueError("Evaluator identity mismatch")
            return result

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(run_evaluator, EVALUATORS))

        # Deterministic rules override an LLM compliance approval.
        violations = check_commercial_rules(
            draft,
            metadata.get("client_country"),
        )
        violations.extend(
            check_client_identity(
                draft,
                metadata.get("client_name"),
            )
        )

        if violations:
            compliance_index = EVALUATORS.index("compliance")
            compliance = results[compliance_index]

            results[compliance_index] = EvaluationResult(
                evaluator="compliance",
                passed=False,
                score=0.0,
                feedback=[
                    *compliance.feedback,
                    "Correct the detected commercial violations.",
                ],
                violations=[
                    *compliance.violations,
                    *violations,
                ],
            )

        evaluations = [
            result.model_dump() for result in results
        ]

        passed = all(result.passed for result in results)

        history.append({
            "iteration": iteration,
            "evaluations": evaluations,
        })

        final = {
            "department_id": department_id,
            "draft_content": draft,
            "currency": generated["currency"],
            "missing_information": generated.get(
                "missing_information", []
            ),
            "iterations": iteration,
            "evaluation_results": {
                "passed": passed,
                "evaluators": evaluations,
                "history": history,
            },
        }

        if passed:
            final["review_status"] = "evaluation_passed"
            return final

        issues = []
        for result in results:
            if not result.passed:
                issues.extend(result.feedback)
                issues.extend(result.violations)

        feedback = "\n".join(
            f"- {issue}" for issue in issues
        )

    final["review_status"] = "needs_human_review"
    return final
