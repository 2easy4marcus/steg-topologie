from fastapi import APIRouter, Depends

from app.request_metrics import metrics


def create_router(verify_ops_secret):
    router = APIRouter(
        prefix="/api/internal/ops",
        dependencies=[Depends(verify_ops_secret)],
    )

    @router.get("/summary")
    def summary():
        return metrics.summary()

    return router
