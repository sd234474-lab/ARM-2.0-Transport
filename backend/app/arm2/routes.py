from fastapi import APIRouter

from app.auth import CurrentUser

router = APIRouter(prefix="/api/arm2", tags=["arm2"])


@router.get("/status")
def arm2_status(_: CurrentUser):
    return {"module": "arm2", "phase": "bootstrap"}
