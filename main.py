import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.config import settings
from routes import auth as auth_router
from routes import contracts as contracts_router
from routes.auth import require_session
from services import auth_service
from services import migration_platform_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).parent

app = FastAPI(
    title="Contract Migration Review",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── Router registration ─────────────────────────────────────────────────
# Auth endpoints are PUBLIC (login must be reachable without a session).
app.include_router(auth_router.router, prefix="/api")

# All /api/contracts, /api/excluded, /api/migrate, /api/exclude, /api/restore
# endpoints require a valid session cookie.  We attach the dependency at
# router-inclusion time so individual handlers don't need to change.
app.include_router(
    contracts_router.router,
    prefix="/api",
    dependencies=[Depends(require_session)],
)


# ── Page routes ─────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page — always renders.  If the user already has a valid
    session, bounce them back into the app so they don't have to re-type."""
    sid = request.cookies.get(settings.APP_SESSION_COOKIE)
    if sid and auth_service.get_session(sid):
        return RedirectResponse(url="/", status_code=302)
    # Use the new keyword-based TemplateResponse signature (Starlette >= 0.29).
    # Passing `request` positionally as the first argument is required in
    # newer Starlette releases; the old (name, context) form still works via
    # kwargs on 0.38.x but is deprecated and breaks on some Azure images.
    return templates.TemplateResponse(
        request=request, name="login.html", context={}
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Root — requires a valid session cookie.  Anonymous users are
    redirected to /login (this is the "protected access" requirement)."""
    sid = request.cookies.get(settings.APP_SESSION_COOKIE)
    if not sid or not auth_service.get_session(sid):
        return RedirectResponse(url="/login", status_code=302)
    # Same keyword-based signature — see /login handler above.
    return templates.TemplateResponse(
        request=request, name="index.html", context={}
    )


# ── Lifecycle hooks ─────────────────────────────────────────────────────
@app.on_event("shutdown")
def _shutdown_close_db_pool() -> None:
    """Close the migration-platform PostgreSQL connection pool cleanly on
    process shutdown so sockets are returned instead of GC'd."""
    try:
        migration_platform_db.close_pool()
    except Exception:
        # Never let a pool-close failure hide a real shutdown error.
        logging.getLogger(__name__).exception("close_pool during shutdown failed")
