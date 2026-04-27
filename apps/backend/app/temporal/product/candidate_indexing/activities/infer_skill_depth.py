import json

from temporalio import activity

from app.core.llm.base import LLMMessage
from app.core.llm.factory import get_llm_client
from app.schemas.product.candidate import CandidateProfile, GitHubSignals, Skill
from app.temporal.core.activity_registry import ActivityRegistry
from app.utils.logging import get_logger

LOGGER = get_logger(__name__)

_SYSTEM_PROMPT = """You are a skill depth analyzer. Given a candidate's claimed skills and their GitHub language statistics, re-tag each skill's depth.

Depth rules (apply strictly):
- "evidenced_commits": skill name matches a language in github_languages AND total_commits > 100
- "evidenced_projects": skill name matches a language in github_languages but total_commits <= 100
- "claimed_only": skill name NOT found in github_languages, OR no GitHub data available

Matching is case-insensitive. Common mappings: "Python" -> "Python", "TypeScript" -> "TypeScript", "JavaScript" -> "JavaScript", "Go" -> "Go", "Rust" -> "Rust", "Java" -> "Java".

Return a JSON array of objects with fields: name (string), depth (string).
Return ALL skills from the input - do not drop any."""


@ActivityRegistry.register("candidate_indexing", "infer_skill_depth")
@activity.defn
async def infer_skill_depth(profile_data: dict, github_signals_data: dict) -> dict:
    """Re-tag skill depth using GitHub language evidence. Returns updated profile_data."""
    profile = CandidateProfile.model_validate(profile_data)
    github = GitHubSignals.model_validate(github_signals_data) if github_signals_data else GitHubSignals()

    # Graceful skip - no GitHub data, all skills stay claimed_only
    if github.is_empty() or not profile.skills:
        LOGGER.info(
            "Skipping skill depth inference - no GitHub data or no skills",
            extra={"candidate_name": profile.full_name, "github_empty": github.is_empty()},
        )
        return profile.model_dump(mode="json")

    LOGGER.info(
        "Inferring skill depths",
        extra={
            "candidate_name": profile.full_name,
            "skills": len(profile.skills),
            "languages": len(github.languages),
        },
    )

    skills_input = json.dumps([{"name": s.name, "depth": s.depth} for s in profile.skills])
    github_input = json.dumps({
        "github_languages": github.languages,
        "total_commits": github.commits_12m,
    })

    llm = get_llm_client()

    response = await llm.complete(
        messages=[
            LLMMessage(role="system", content=_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=f"Skills: {skills_input}\n\nGitHub data: {github_input}",
            ),
        ],
        temperature=0.0,
    )

    try:
        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        updated_skills_data = json.loads(raw)
        updated_skills = [Skill.model_validate(s) for s in updated_skills_data]
        # Validate count matches - if LLM dropped skills, fall back to original
        if len(updated_skills) != len(profile.skills):
            LOGGER.warning(
                "Skill count mismatch after depth inference - using original",
                extra={"original": len(profile.skills), "returned": len(updated_skills)},
            )
            updated_skills = profile.skills
    except Exception as e:
        LOGGER.warning(
            "Failed to parse skill depth response - using original",
            extra={"error": str(e)},
        )
        updated_skills = profile.skills

    profile.skills = updated_skills

    LOGGER.info(
        "Skill depths updated",
        extra={
            "candidate_name": profile.full_name,
            "evidenced_commits": sum(1 for s in updated_skills if s.depth == "evidenced_commits"),
            "evidenced_projects": sum(1 for s in updated_skills if s.depth == "evidenced_projects"),
        },
    )

    return profile.model_dump(mode="json")
