"""Smoke tests — models import, migration round-trip, repo CRUD, pgvector insert."""

import hashlib
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Base,
    CompanyUser,
)
from app.repositories.assignments import AssignmentRepository
from app.repositories.candidates import CandidateRepository
from app.repositories.companies import CompanyRepository
from app.repositories.hitl_events import HitlEventRepository
from app.repositories.jobs import JobRepository
from app.repositories.operators import OperatorRepository
from app.repositories.recruiters import RecruiterRepository
from app.repositories.rubrics import RubricRepository
from app.repositories.scorecards import ScorecardRepository
from app.repositories.submissions import CandidateSubmissionRepository
from app.repositories.workflow_runs import WorkflowRunRepository

# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _dedup_hash(name: str, email: str | None = None) -> str:
    raw = f"{name.lower()}:{(email or '').lower()}"
    return hashlib.md5(raw.encode()).hexdigest()


# ------------------------------------------------------------------ #
# 1. Metadata sanity                                                  #
# ------------------------------------------------------------------ #


def test_all_tables_registered():
    tables = sorted(Base.metadata.tables.keys())
    assert tables == [
        "assignments",
        "candidate_submissions",
        "candidates",
        "companies",
        "company_users",
        "hitl_events",
        "jobs",
        "operators",
        "recruiters",
        "rubrics",
        "scorecards",
        "workflow_runs",
    ]


# ------------------------------------------------------------------ #
# 2. Company + CompanyUser                                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_company(session: AsyncSession):
    repo = CompanyRepository(session)
    company = await repo.create(
        name="Acme Corp",
        stage="series_a",
        industry="SaaS",
        status="active",
    )
    assert company.id is not None
    fetched = await repo.get_by_id(company.id)
    assert fetched is not None
    assert fetched.name == "Acme Corp"
    assert fetched.stage == "series_a"


@pytest.mark.asyncio
async def test_create_company_user(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="Beta Inc", status="active")

    user = CompanyUser(
        company_id=company.id,
        email="hiring@beta.com",
        full_name="Alice Hiring",
        role="hiring_manager",
        supabase_user_id=str(uuid.uuid4()),
    )
    session.add(user)
    await session.flush()
    await session.commit()

    assert user.id is not None
    assert user.company_id == company.id


# ------------------------------------------------------------------ #
# 3. Operator                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_operator(session: AsyncSession):
    repo = OperatorRepository(session)
    op = await repo.create(
        email=f"ops_{uuid.uuid4().hex[:6]}@contrario.ai",
        full_name="Talent Ops",
        status="active",
    )
    assert op.id is not None
    fetched = await repo.get_by_id(op.id)
    assert fetched.email == op.email


# ------------------------------------------------------------------ #
# 4. Recruiter + embedding                                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_recruiter_with_embedding(session: AsyncSession):
    repo = RecruiterRepository(session)
    embedding = [0.1] * 384  # mock 384-dim vector
    recruiter = await repo.create(
        full_name="Bob Recruiter",
        email=f"bob_{uuid.uuid4().hex[:6]}@agency.com",
        domain_expertise=["engineering", "fintech"],
        status="active",
        total_placements=12,
        at_capacity=False,
        embedding=embedding,
    )
    assert recruiter.id is not None
    fetched = await repo.get_by_email(recruiter.email)
    assert fetched is not None
    assert fetched.domain_expertise == ["engineering", "fintech"]
    assert fetched.embedding is not None


@pytest.mark.asyncio
async def test_recruiter_search_by_domain(session: AsyncSession):
    repo = RecruiterRepository(session)
    await repo.create(
        full_name="Jane Engineer",
        email=f"jane_{uuid.uuid4().hex[:6]}@agency.com",
        domain_expertise=["engineering"],
        status="active",
        total_placements=5,
        at_capacity=False,
    )
    results = await repo.search_by_domain("engineering")
    assert len(results) >= 1


# ------------------------------------------------------------------ #
# 5. Candidate + dedup + pgvector cosine query                        #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_candidate(session: AsyncSession):
    repo = CandidateRepository(session)
    embedding = [0.2] * 384
    candidate = await repo.create(
        full_name="Carol Dev",
        email="carol@example.com",
        seniority="senior",
        years_experience=7,
        skills=[{"name": "Python", "depth": "evidenced_commits"}],
        completeness_score=Decimal("0.85"),
        embedding=embedding,
        source="seed",
        dedup_hash=_dedup_hash("Carol Dev", "carol@example.com"),
        status="indexed",
    )
    assert candidate.id is not None

    by_hash = await repo.get_by_dedup_hash(candidate.dedup_hash)
    assert by_hash is not None
    assert by_hash.full_name == "Carol Dev"


@pytest.mark.asyncio
async def test_candidate_github_dedup(session: AsyncSession):
    repo = CandidateRepository(session)
    await repo.create(
        full_name="Dev Gopher",
        github_username="devgopher",
        completeness_score=Decimal("0.6"),
        source="sourcing_agent",
        dedup_hash=_dedup_hash("Dev Gopher"),
        status="indexed",
    )
    found = await repo.get_by_github_username("devgopher")
    assert found is not None
    assert found.github_username == "devgopher"


@pytest.mark.asyncio
async def test_pgvector_cosine_query(session: AsyncSession):
    """Confirm pgvector cosine similarity query executes against the table."""
    result = await session.execute(
        text(
            "SELECT id FROM candidates "
            "ORDER BY embedding <=> CAST(:q AS vector) "
            "LIMIT 5"
        ),
        {"q": str([0.0] * 384)},
    )
    # Just confirm it returns without error (pool may be empty in test isolation)
    rows = result.fetchall()
    assert isinstance(rows, list)


# ------------------------------------------------------------------ #
# 6. Job + Rubric                                                     #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_job_and_rubric(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="StartupXYZ", status="active")

    job_repo = JobRepository(session)
    job = await job_repo.create(
        company_id=company.id,
        title="Founding Engineer",
        jd_text="Build our core platform from scratch.",
        role_category="engineering",
        seniority_level="senior",
        status="intake",
    )
    assert job.id is not None

    rubric_repo = RubricRepository(session)
    rubric = await rubric_repo.create(
        job_id=job.id,
        version=1,
        dimensions=[
            {"name": "distributed_systems", "weight": 0.25, "description": "..."},
        ],
    )
    assert rubric.id is not None

    latest = await rubric_repo.get_latest_for_job(job.id)
    assert latest is not None
    assert latest.version == 1

    job_with_rubric = await job_repo.get_with_rubric(job.id)
    assert job_with_rubric is not None
    assert len(job_with_rubric.rubrics) == 1


# ------------------------------------------------------------------ #
# 7. Assignment + operator confirm (HITL #1)                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_assignment_operator_confirm(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="ConfirmCorp", status="active")

    job_repo = JobRepository(session)
    job = await job_repo.create(
        company_id=company.id,
        title="ML Engineer",
        jd_text="ML stuff.",
        status="recruiter_assignment",
    )

    recruiter_repo = RecruiterRepository(session)
    recruiter = await recruiter_repo.create(
        full_name="Sam Recruiter",
        email=f"sam_{uuid.uuid4().hex[:6]}@firm.com",
        domain_expertise=["engineering"],
        status="active",
        total_placements=8,
        at_capacity=False,
    )

    op_repo = OperatorRepository(session)
    operator = await op_repo.create(
        email=f"op_{uuid.uuid4().hex[:6]}@contrario.ai",
        status="active",
    )

    assignment_repo = AssignmentRepository(session)
    assignment = await assignment_repo.create(
        job_id=job.id,
        recruiter_id=recruiter.id,
        ai_score=Decimal("87.5"),
        confidence=Decimal("0.92"),
        operator_override=False,
        status="recommended",
    )
    assert assignment.status == "recommended"

    confirmed = await assignment_repo.set_operator_confirmed(assignment.id, operator.id)
    assert confirmed is not None
    assert confirmed.status == "operator_confirmed"
    assert confirmed.confirmed_by_operator_id == operator.id
    assert confirmed.confirmed_at is not None


# ------------------------------------------------------------------ #
# 8. CandidateSubmission                                              #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_candidate_submission(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="SubCorp", status="active")

    job_repo = JobRepository(session)
    job = await job_repo.create(
        company_id=company.id,
        title="Backend Dev",
        jd_text="Build APIs.",
        status="sourcing",
    )

    recruiter_repo = RecruiterRepository(session)
    recruiter = await recruiter_repo.create(
        full_name="Eve Recruiter",
        email=f"eve_{uuid.uuid4().hex[:6]}@firm.com",
        domain_expertise=["engineering"],
        status="active",
        total_placements=3,
        at_capacity=False,
    )

    candidate_repo = CandidateRepository(session)
    candidate = await candidate_repo.create(
        full_name="Frank Candidate",
        completeness_score=Decimal("0.75"),
        source="recruiter_upload",
        dedup_hash=_dedup_hash("Frank Candidate"),
        status="indexed",
    )

    submission_repo = CandidateSubmissionRepository(session)
    submission = await submission_repo.create(
        job_id=job.id,
        candidate_id=candidate.id,
        recruiter_id=recruiter.id,
        status="submitted",
    )
    assert submission.id is not None

    found = await submission_repo.get_by_job_candidate(job.id, candidate.id)
    assert found is not None
    assert found.status == "submitted"


# ------------------------------------------------------------------ #
# 9. Scorecard + upsert idempotency                                   #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_scorecard_upsert(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="ScorecardCo", status="active")

    job_repo = JobRepository(session)
    job = await job_repo.create(
        company_id=company.id,
        title="Data Scientist",
        jd_text="ML and data stuff.",
        status="scoring",
    )

    rubric_repo = RubricRepository(session)
    rubric = await rubric_repo.create(
        job_id=job.id,
        version=1,
        dimensions=[{"name": "ml_depth", "weight": 0.5}],
    )

    candidate_repo = CandidateRepository(session)
    candidate = await candidate_repo.create(
        full_name="Grace ML",
        completeness_score=Decimal("0.9"),
        source="seed",
        dedup_hash=_dedup_hash("Grace ML"),
        status="indexed",
    )

    scorecard_repo = ScorecardRepository(session)
    scorecard = await scorecard_repo.upsert_by_key(
        job_id=job.id,
        candidate_id=candidate.id,
        rubric_id=rubric.id,
        overall_match_score=Decimal("91.0"),
        dimensions=[{"name": "ml_depth", "score": 91}],
        self_correction_triggered=False,
    )
    assert scorecard.overall_match_score == Decimal("91.0")

    # Upsert again — should update, not insert duplicate
    updated = await scorecard_repo.upsert_by_key(
        job_id=job.id,
        candidate_id=candidate.id,
        rubric_id=rubric.id,
        overall_match_score=Decimal("93.5"),
        dimensions=[{"name": "ml_depth", "score": 93}],
        self_correction_triggered=True,
    )
    assert updated.overall_match_score == Decimal("93.5")
    assert updated.self_correction_triggered is True

    # Unique constraint — only one row
    all_cards = await scorecard_repo.get_by_job(job.id)
    assert len(all_cards) == 1


# ------------------------------------------------------------------ #
# 10. HitlEvent audit log                                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_hitl_event(session: AsyncSession):
    company_repo = CompanyRepository(session)
    company = await company_repo.create(name="HitlCo", status="active")

    job_repo = JobRepository(session)
    job = await job_repo.create(
        company_id=company.id,
        title="PM",
        jd_text="Manage products.",
        status="review",
    )

    hitl_repo = HitlEventRepository(session)
    actor_id = uuid.uuid4()
    event = await hitl_repo.create(
        job_id=job.id,
        signal_type="company_review",
        actor_type="company_user",
        actor_id=actor_id,
        action="approve",
        payload={"candidate_id": str(uuid.uuid4())},
        workflow_id="wf-test-123",
    )
    assert event.id is not None

    events = await hitl_repo.get_by_job(job.id)
    assert len(events) == 1
    assert events[0].signal_type == "company_review"


# ------------------------------------------------------------------ #
# 11. WorkflowRun                                                     #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_workflow_run(session: AsyncSession):
    repo = WorkflowRunRepository(session)
    wf_id = f"JobIntakeWorkflow:{uuid.uuid4()}"
    run = await repo.create(
        workflow_id=wf_id,
        workflow_type="JobIntakeWorkflow",
        status="running",
    )
    assert run.id is not None

    found = await repo.get_by_workflow_id(wf_id)
    assert found is not None
    assert found.status == "running"
