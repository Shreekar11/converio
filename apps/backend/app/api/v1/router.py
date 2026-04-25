from fastapi import APIRouter

from app.api.v1.endpoints import candidates, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["Health"])
api_router.include_router(candidates.router, prefix="/candidates", tags=["Candidates"])
