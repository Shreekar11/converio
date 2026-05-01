from fastapi import APIRouter

from app.api.v1.endpoints import candidates, health, recruiters

api_router = APIRouter()
api_router.include_router(health.router, tags=["Health"])
api_router.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
api_router.include_router(recruiters.router, prefix="/recruiters", tags=["Recruiters"])
