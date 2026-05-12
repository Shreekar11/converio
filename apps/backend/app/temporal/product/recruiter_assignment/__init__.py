"""Recruiter Assignment Agent (Agent 0) — import triggers activity/workflow registration."""
# Import all activities to register them with ActivityRegistry and Temporal
from app.temporal.product.recruiter_assignment.activities import (  # noqa: F401
    assign_recruiters_to_role,
    check_recent_placements,
    format_recruiter_recommendations,
    log_hitl_event,
    notify_assigned_recruiters,
    persist_proposal,
    query_recruiter_capacity,
    rank_and_select_recruiters,
    relax_stage_match,
    score_recruiter_fit,
    search_recruiter_pool,
    summarize_recruiter_track_record,
    synthesize_best_effort_proposal,
    transition_job_status,
    widen_domain_search,
)

# Import workflow to register with WorkflowRegistry
from app.temporal.product.recruiter_assignment.workflows import recruiter_assignment_workflow  # noqa: F401
