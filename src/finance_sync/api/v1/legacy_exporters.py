"""Clear migration response for retired global exporter endpoints."""

from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(prefix="/exporters", tags=["exporters (deprecated)"])


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def retired_exporters(request: Request, path: str) -> None:
    """Direct callers to persisted destinations instead of env configuration."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail=(
            "Global exporter configuration is retired. Create and manage a "
            "persisted destination through /api/v1/destinations."
        ),
    )
