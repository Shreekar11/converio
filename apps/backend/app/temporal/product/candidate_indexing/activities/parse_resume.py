import base64

from temporalio import activity

from app.core.document_parser import parse_document
from app.core.llm.base import LLMMessage
from app.core.llm.factory import get_llm_client
from app.schemas.product.candidate import CandidateProfile
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_SYSTEM_PROMPT = """You are a resume parser. Extract structured candidate information from the resume text provided.

Rules:
- Extract all skills mentioned. Set depth="claimed_only" for ALL skills (GitHub evidence is assessed separately).
- For work_history: extract company name, role_title, start_date (YYYY-MM or YYYY), end_date (YYYY-MM or YYYY, null if current).
- For education: extract institution, degree, field_of_study, graduation_year (integer).
- seniority: infer from years of experience and role titles. Use one of: junior, mid, senior, staff, principal.
- stage_fit: infer from company sizes/stages in work history. Use values from: seed, series_a, series_b, series_c, growth.
- If a field cannot be determined, use null (not empty string).
- resume_text: copy the full raw markdown text here for downstream citation resolution.
- Do not hallucinate. Only extract what is present in the resume."""


@ActivityRegistry.register("candidate_indexing", "parse_resume")
@activity.defn
async def parse_resume(raw_bytes_b64: str, mime_type: str) -> dict:
    """Step 1: decode bytes -> Markdown via docling. Step 2: LLM extracts CandidateProfile."""
    raw_bytes = base64.b64decode(raw_bytes_b64)

    LOGGER.info("Parsing document", extra={"mime_type": mime_type, "bytes": len(raw_bytes)})

    # Step 1: docling -> Markdown (deterministic, no LLM)
    markdown = await parse_document(raw_bytes, mime_type)

    LOGGER.info("Document parsed to markdown", extra={"markdown_len": len(markdown)})

    # Step 2: LLM structured extraction
    llm = get_llm_client()
    profile = await llm.structured_complete(
        messages=[
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(role="user", content=markdown),
        ],
        schema=CandidateProfile,
    )

    # Store the original markdown as resume_text for downstream citation resolution
    if not profile.resume_text:
        profile.resume_text = markdown

    LOGGER.info(
        "Resume parsed",
        extra={
            "name": profile.full_name,
            "skills": len(profile.skills),
            "work_history": len(profile.work_history),
        },
    )

    return profile.model_dump(mode="json")
