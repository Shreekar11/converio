from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    candidates,
    companies,
    health,
    jobs,
    recruiter_assignment,
    recruiters,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["Health"])
api_router.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
api_router.include_router(companies.router, prefix="/companies", tags=["Companies"])
api_router.include_router(jobs.router, prefix="/jobs", tags=["Jobs"])
api_router.include_router(
    recruiter_assignment.router, prefix="/jobs", tags=["Recruiter Assignment"]
)
api_router.include_router(recruiters.router, prefix="/recruiters", tags=["Recruiters"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
