"""FastAPI router that serves the GUI control panel.

This router mounts Jinja2 templates and serves the login page and
the main dashboard application.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def gui_index(request: Request) -> HTMLResponse:
    """Serve the main dashboard page.

    If the request includes a query param or header indicating the user
    isn't logged in, we could redirect to login.  For simplicity, the
    client-side JS handles auth state and redirects to /login if no
    valid token is found.
    """
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def gui_login(request: Request) -> HTMLResponse:
    """Serve the standalone login page."""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
    )
