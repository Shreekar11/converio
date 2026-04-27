from temporalio import activity

from app.core.document_parser import parse_document
from app.core.storage.supabase_storage import get_supabase_storage_client
from app.core.llm.base import LLMMessage
from app.core.llm.factory import get_llm_client
from app.schemas.product.candidate import CandidateProfile, ResumeFileRef
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
async def parse_resume(resume_file_data: dict) -> dict:
    """Step 1: fetch object bytes -> Markdown via docling. Step 2: LLM extracts CandidateProfile."""
    resume_file = ResumeFileRef.model_validate(resume_file_data)
    storage = get_supabase_storage_client()
    raw_bytes = await storage.download_bytes(bucket=resume_file.bucket, path=resume_file.path)

    LOGGER.info(
        "Parsing document",
        extra={
            "bucket": resume_file.bucket,
            "path": resume_file.path,
            "mime_type": resume_file.mime_type,
            "bytes": len(raw_bytes),
        },
    )

    # Step 1: docling -> Markdown (deterministic, no LLM)
    markdown = await parse_document(raw_bytes, resume_file.mime_type)

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
            "candidate_name": profile.full_name,
            "skills": len(profile.skills),
            "work_history": len(profile.work_history),
        },
    )

    return profile.model_dump(mode="json")
