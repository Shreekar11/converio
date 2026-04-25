"""Pydantic schemas for Agent 2 (Candidate Indexing Workflow) IO.

All models must be JSON-serializable — no ORM objects, no non-serializable types.
Activity inputs/outputs and workflow IO use these models exclusively.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GitHubSignals(BaseModel):
    """GitHub public signals fetched by fetch_github_signals activity."""
    repo_count: int = 0
    top_language: str | None = None
    commits_12m: int = 0
    stars_total: int = 0
    languages: dict[str, int] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        return self.repo_count == 0


class Skill(BaseModel):
    """Candidate skill with evidence depth tag."""
    name: str
    depth: Literal["claimed_only", "evidenced_projects", "evidenced_commits"] = "claimed_only"


class WorkHistoryItem(BaseModel):
    """Single work history entry parsed from resume."""
    company: str
    role_title: str
    start_date: str | None = None  # ISO date string or freeform
    end_date: str | None = None    # None = current
    description: str | None = None


class EducationItem(BaseModel):
    """Single education entry parsed from resume."""
    institution: str
    degree: str | None = None
    field_of_study: str | None = None
    graduation_year: int | None = None


class CandidateProfile(BaseModel):
    """Structured candidate profile. Output of parse_resume + infer_skill_depth activities.

    Skills start as claimed_only; infer_skill_depth re-tags using GitHub evidence.
    """
    full_name: str
    email: str | None = None
    phone: str | None = None
    github_username: str | None = None
    linkedin_url: str | None = None
    location: str | None = None
    seniority: Literal["junior", "mid", "senior", "staff", "principal"] | None = None
    years_experience: int | None = None
    stage_fit: list[str] = Field(default_factory=list)  # ["seed", "series_a", ...]
    skills: list[Skill] = Field(default_factory=list)
    work_history: list[WorkHistoryItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    resume_text: str | None = None  # raw text for citation resolver (downstream agents)


class CandidateIndexingInput(BaseModel):
    """Input to CandidateIndexingWorkflow."""
    raw_bytes_b64: str          # base64-encoded file bytes
    mime_type: str              # application/pdf | application/vnd.openxmlformats... | text/markdown
    source: str                 # "seed" | "recruiter_upload" | "sourcing_agent"
    source_recruiter_id: str | None = None  # UUID string; null for seed/sourcing


class ResolveDuplicatesResult(BaseModel):
    """Output of resolve_entity_duplicates activity."""
    is_duplicate: bool
    existing_candidate_id: str | None = None
    match_source: Literal["dedup_hash", "github_username"] | None = None


class IndexingResult(BaseModel):
    """Output of CandidateIndexingWorkflow — returned to caller."""
    candidate_id: str
    status: Literal["indexed", "review_queue", "failed"]
    completeness_score: float
    was_duplicate: bool
    source: str
