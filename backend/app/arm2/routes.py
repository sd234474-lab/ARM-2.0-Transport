from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth import require_permission
from app.permissions import Permission

router = APIRouter(prefix="/api/arm2", tags=["arm2"])
Arm2TypeViewer = Annotated[
    object,
    Depends(require_permission(Permission.ARM2_TYPE_VIEW)),
]


@router.get("/status")
def arm2_status(_: Arm2TypeViewer):
    return {"module": "arm2", "phase": "bootstrap"}
