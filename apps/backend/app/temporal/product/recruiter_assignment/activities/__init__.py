"""Recruiter assignment activities — import triggers @activity.defn registration."""
from app.temporal.product.recruiter_assignment.activities.assign_recruiters_to_role import assign_recruiters_to_role  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.check_recent_placements import check_recent_placements  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.format_recruiter_recommendations import format_recruiter_recommendations  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.log_hitl_event import log_hitl_event  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.notify_assigned_recruiters import notify_assigned_recruiters  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.persist_proposal import persist_proposal  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.query_recruiter_capacity import query_recruiter_capacity  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.rank_and_select_recruiters import rank_and_select_recruiters  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.relax_stage_match import relax_stage_match  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.score_recruiter_fit import score_recruiter_fit  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.search_recruiter_pool import search_recruiter_pool  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.summarize_recruiter_track_record import summarize_recruiter_track_record  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.synthesize_best_effort_proposal import synthesize_best_effort_proposal  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.transition_job_status import transition_job_status  # noqa: F401
from app.temporal.product.recruiter_assignment.activities.widen_domain_search import widen_domain_search  # noqa: F401
