"""Isolated tests for GET /rfp/tickets/{id}/departments/{dept}/review.

Everything runs against an in-memory SQLite database with synthetic data.
No PostgreSQL, TinyDB writes, Redis, checkpoints or network are used.
"""

import inspect
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import rfp_approval_service
from rfp_approval_auth import APPROVER_EMAIL_ENV
from rfp_models import RFPDepartmentSection, RFPMetadata, RFPTicket
from routers import rfp_intake
from security import get_current_user


TICKET_ID = "rfp-review-ticket-001"
OWNER_UUID = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
DEPARTMENTS = ["warehouse", "lastmile", "reverse"]

APPROVER_EMAILS = {
    dept: f"trackflow-demo-v2-{dept}@example.invalid"
    for dept in DEPARTMENTS
}
LEGACY_EMAILS = {
    dept: f"trackflow-demo-{dept}@example.invalid"
    for dept in DEPARTMENTS
}

SECRETS = [
    OWNER_UUID,
    "CLIENTE-SECRETO-SINTETICO",
    "PAIS-SECRETO-SINTETICO",
    "/secreto/ruta/rfp-original.pdf",
    "NOTA-INTERNA-SECRETA",
    "Nombre Aprobador Secreto",
    "BORRADOR-LASTMILE-SINTETICO",
    "BORRADOR-REVERSE-SINTETICO",
]

WHITELIST = {
    "ticket_id",
    "department_id",
    "ticket_status",
    "draft_content",
    "generation_status",
    "review_status",
    "approval_status",
}


def make_user(email, *, active=True, uuid=None, role="user"):
    return SimpleNamespace(
        id=1,
        uuid=uuid or f"uuid-of-{email}",
        email=email,
        is_active=active,
        role=role,
    )


class RecordingSession(Session):
    """Real ORM session that records every write or locking attempt."""

    log = None

    def exec(self, statement, *args, **kwargs):
        if self.new or self.dirty or self.deleted:
            self.log["writes"].append("pending-changes")
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.log["statements"].append(compiled)
        return super().exec(statement, *args, **kwargs)

    def get(self, *args, **kwargs):
        if kwargs.get("with_for_update"):
            self.log["locks"].append("get(with_for_update)")
        return super().get(*args, **kwargs)

    def add(self, *args, **kwargs):
        self.log["writes"].append("add")
        return super().add(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.log["writes"].append("delete")
        return super().delete(*args, **kwargs)

    def commit(self):
        self.log["writes"].append("commit")
        return super().commit()


def snapshot(engine):
    with Session(engine) as session:
        return [
            [
                dict(row)
                for row in (
                    session.exec(select(model)).all()
                )
            ]
            for model in (RFPTicket, RFPMetadata, RFPDepartmentSection)
        ]


def seed(engine, *, status="needs_human_review", active=None, drafts=None):
    active = DEPARTMENTS if active is None else active
    drafts = drafts or {
        "warehouse": "BORRADOR-WAREHOUSE-SINTETICO",
        "lastmile": "BORRADOR-LASTMILE-SINTETICO",
        "reverse": "BORRADOR-REVERSE-SINTETICO",
    }

    with Session(engine) as session:
        session.add(RFPTicket(
            ticket_id=TICKET_ID,
            status=status,
            raw_pdf_path="/secreto/ruta/rfp-original.pdf",
            owner_uuid=OWNER_UUID,
            created_at=datetime.now(timezone.utc),
        ))
        session.add(RFPMetadata(
            rfp_id="rfp-review-meta-001",
            ticket_id=TICKET_ID,
            client_name="CLIENTE-SECRETO-SINTETICO",
            client_country="PAIS-SECRETO-SINTETICO",
            departments_needed=list(active),
        ))
        for dept in DEPARTMENTS:
            session.add(RFPDepartmentSection(
                id=f"section-{dept}",
                ticket_id=TICKET_ID,
                department_id=dept,
                key_aspects=["ASPECTO-INTERNO"],
                draft_content=drafts.get(dept),
                evaluation_results={
                    "generation_status": "completed",
                    "review_status": "needs_human_review",
                    "feedback": "NOTA-INTERNA-SECRETA",
                    "verified_commercial_metrics": {"monthly_capacity": 999},
                },
                approval_status=None,
                approver="Nombre Aprobador Secreto",
            ))
        session.commit()


@pytest.fixture
def env(monkeypatch):
    for dept, email in APPROVER_EMAILS.items():
        monkeypatch.setenv(APPROVER_EMAIL_ENV[dept], email)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            RFPTicket.__table__,
            RFPMetadata.__table__,
            RFPDepartmentSection.__table__,
        ],
    )

    log = {
        "statements": [],
        "writes": [],
        "locks": [],
        "commits": [],
        "sessions": 0,
        "engines": 0,
    }
    RecordingSession.log = log
    event.listen(engine, "commit", lambda conn: log["commits"].append(1))

    def get_engine():
        log["engines"] += 1
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def session_factory(_engine):
        log["sessions"] += 1
        return RecordingSession(engine)

    app = FastAPI()
    app.include_router(rfp_intake.router)

    with (
        patch.object(rfp_approval_service, "get_engine", get_engine),
        patch.object(rfp_approval_service, "Session", session_factory),
        patch.object(rfp_intake, "get_engine", get_engine),
        patch.object(rfp_intake, "Session", session_factory),
        TestClient(app) as client,
    ):
        yield SimpleNamespace(
            app=app,
            client=client,
            engine=engine,
            log=log,
            seed=lambda **kw: seed(engine, **kw),
        )

    app.dependency_overrides.clear()
    RecordingSession.log = None


def as_user(env, user):
    env.app.dependency_overrides[get_current_user] = lambda: user


def review_url(department, ticket_id=TICKET_ID):
    return f"/rfp/tickets/{ticket_id}/departments/{department}/review"


def clear_log(env):
    for key in ("statements", "writes", "locks", "commits"):
        env.log[key].clear()
    env.log["sessions"] = 0
    env.log["engines"] = 0


# A
def test_anonymous_is_rejected(env):
    env.seed()
    response = env.client.get(review_url("warehouse"))
    assert response.status_code == 401
    assert env.log["sessions"] == 0


# B
@pytest.mark.parametrize("approver", DEPARTMENTS)
@pytest.mark.parametrize("department", DEPARTMENTS)
def test_approver_matrix_own_200_others_403(env, approver, department):
    env.seed()
    as_user(env, make_user(APPROVER_EMAILS[approver]))

    response = env.client.get(review_url(department))

    if approver == department:
        assert response.status_code == 200
        assert response.json()["department_id"] == department
    else:
        assert response.status_code == 403
        assert "BORRADOR" not in response.text


# C
def test_regular_user_is_rejected(env):
    env.seed()
    as_user(env, make_user("cualquiera@example.invalid"))
    assert env.client.get(review_url("warehouse")).status_code == 403


# D
def test_owner_who_is_not_approver_is_rejected(env):
    env.seed()
    as_user(
        env,
        make_user("propietario@example.invalid", uuid=OWNER_UUID),
    )

    for dept in DEPARTMENTS:
        assert env.client.get(review_url(dept)).status_code == 403


# E
def test_inactive_approver_is_rejected(env):
    env.seed()
    as_user(env, make_user(APPROVER_EMAILS["warehouse"], active=False))
    assert env.client.get(review_url("warehouse")).status_code == 403


# F
@pytest.mark.parametrize("department", DEPARTMENTS)
def test_legacy_v1_account_is_rejected(env, department):
    env.seed()
    as_user(env, make_user(LEGACY_EMAILS[department]))
    assert env.client.get(review_url(department)).status_code == 403


# G
def test_missing_ticket_returns_404(env):
    env.seed()
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))
    response = env.client.get(review_url("warehouse", "no-existe"))
    assert response.status_code == 404


def test_unknown_department_returns_404_without_query(env):
    env.seed()
    clear_log(env)
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))
    assert env.client.get(review_url("finance")).status_code == 404
    assert env.log["sessions"] == 0


# H
def test_inactive_department_returns_404(env):
    env.seed(active=["warehouse"])
    as_user(env, make_user(APPROVER_EMAILS["reverse"]))
    response = env.client.get(review_url("reverse"))
    assert response.status_code == 404
    assert "BORRADOR" not in response.text


# I
@pytest.mark.parametrize("draft", [None, ""])
def test_missing_draft_returns_409(env, draft):
    env.seed(drafts={"lastmile": draft})
    as_user(env, make_user(APPROVER_EMAILS["lastmile"]))
    assert env.client.get(review_url("lastmile")).status_code == 409


# J
@pytest.mark.parametrize(
    "status",
    [
        "analizando",
        "analisis_completo",
        "generando_borrador",
        "en_evaluación",
        "terminado",
        "descartado",
    ],
)
def test_incompatible_ticket_state_returns_409(env, status):
    env.seed(status=status)
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))

    response = env.client.get(review_url("warehouse"))

    assert response.status_code == 409
    assert "BORRADOR" not in response.text


@pytest.mark.parametrize(
    "status", ["needs_human_review", "esperando_aprobación"]
)
def test_allowed_ticket_states_return_200(env, status):
    env.seed(status=status)
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))

    response = env.client.get(review_url("warehouse"))

    assert response.status_code == 200
    assert response.json()["ticket_status"] == status


# K
@pytest.mark.parametrize("department", DEPARTMENTS)
def test_response_is_exact_whitelist_without_leaks(env, department):
    env.seed()
    as_user(env, make_user(APPROVER_EMAILS[department]))

    response = env.client.get(review_url(department))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == WHITELIST
    assert body["ticket_id"] == TICKET_ID
    assert body["department_id"] == department
    assert body["draft_content"] == (
        f"BORRADOR-{department.upper()}-SINTETICO"
    )
    assert body["generation_status"] == "completed"
    assert body["review_status"] == "needs_human_review"
    assert body["approval_status"] is None

    own_draft = f"BORRADOR-{department.upper()}-SINTETICO"
    for secret in SECRETS:
        if secret != own_draft:
            assert secret not in response.text


def test_response_model_forbids_extra_fields():
    model = rfp_intake.RFPDepartmentReviewResponse

    assert set(model.model_fields) == WHITELIST
    with pytest.raises(Exception):
        model(
            **{field: "x" for field in WHITELIST},
            owner_uuid=OWNER_UUID,
        )


# L
def test_read_is_side_effect_free(env):
    env.seed()
    before = snapshot(env.engine)
    clear_log(env)
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))

    response = env.client.get(review_url("warehouse"))

    assert response.status_code == 200
    assert env.log["statements"], "expected at least one query"
    assert all(
        sql.lstrip().upper().startswith("SELECT")
        for sql in env.log["statements"]
    )
    assert not any(
        "FOR UPDATE" in sql.upper() for sql in env.log["statements"]
    )
    assert env.log["locks"] == []
    assert env.log["writes"] == []
    assert env.log["commits"] == []
    assert snapshot(env.engine) == before


def test_service_source_has_no_lock_or_commit():
    source = inspect.getsource(rfp_approval_service.load_department_review)
    source += inspect.getsource(rfp_approval_service.get_department_review)

    for forbidden in (
        "with_for_update",
        "session.commit(",
        "session.add(",
        "session.begin(",
        "SqliteSaver",
        "checkpoint",
        "transition_ticket",
    ):
        assert forbidden not in source


# M
def test_success_response_is_not_cacheable(env):
    env.seed()
    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))
    response = env.client.get(review_url("warehouse"))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


# N
@pytest.mark.parametrize(
    "user",
    [
        make_user("cualquiera@example.invalid"),
        make_user(LEGACY_EMAILS["warehouse"]),
        make_user(APPROVER_EMAILS["warehouse"], active=False),
        make_user(APPROVER_EMAILS["lastmile"]),
    ],
)
def test_authorization_runs_before_any_database_access(env, user):
    env.seed()
    clear_log(env)
    as_user(env, user)

    response = env.client.get(review_url("warehouse"))

    assert response.status_code == 403
    assert env.log["engines"] == 0
    assert env.log["sessions"] == 0
    assert env.log["statements"] == []


# O
def test_existing_routes_are_preserved_and_review_is_get_only(env):
    routes = {
        (method, route.path)
        for route in rfp_intake.router.routes
        if hasattr(route, "methods")
        for method in route.methods
    }
    base = "/rfp/tickets/{ticket_id}"

    expected = {
        ("POST", "/rfp/intake"),
        ("GET", "/rfp/tickets"),
        ("GET", base),
        ("POST", f"{base}/generate"),
        ("POST", f"{base}/departments/{{department_id}}/approval/start"),
        ("POST", f"{base}/departments/{{department_id}}/approval/decision"),
        ("POST", f"{base}/departments/{{department_id}}/regenerate"),
        ("POST", f"{base}/departments/{{department_id}}/commercial-metrics"),
        ("POST", f"{base}/finalize"),
        ("GET", f"{base}/final/download"),
        ("GET", "/rfp/events"),
        ("GET", f"{base}/departments/{{department_id}}/review"),
    }

    assert expected <= routes
    review_methods = {
        method
        for method, path in routes
        if path == f"{base}/departments/{{department_id}}/review"
    }
    assert review_methods == {"GET"}


def test_owner_ticket_endpoint_is_unchanged(env):
    env.seed()

    as_user(env, make_user("propietario@example.invalid", uuid=OWNER_UUID))
    owner = env.client.get(f"/rfp/tickets/{TICKET_ID}")
    assert owner.status_code == 200
    assert owner.json()["ticket_id"] == TICKET_ID

    as_user(env, make_user(APPROVER_EMAILS["warehouse"]))
    approver = env.client.get(f"/rfp/tickets/{TICKET_ID}")
    assert approver.status_code == 403
