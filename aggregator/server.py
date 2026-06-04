"""
ClusterPulse Aggregator — central server that polls all 3 collectors
via Tailscale IPs, exposes a unified status API, and serves a
GitHub-OAuth-protected dashboard.

Run with:
    uvicorn aggregator.server:app --host 127.0.0.1 --port 9200

Environment variables:
    GITHUB_CLIENT_ID       — GitHub OAuth App client ID
    GITHUB_CLIENT_SECRET   — GitHub OAuth App client secret
    GITHUB_REDIRECT_URI    — OAuth callback (default: /auth/callback, appended to request base URL)
    SESSION_SECRET_KEY     — Secret for signing session cookies (default: random on startup)
    ALLOWED_USERS          — Comma-separated GitHub usernames (default: alrobles)
    CLUSTERPULSE_HOSTNAME  — Public hostname for redirect URI (default: monitor.ecoseek.org)
"""

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES_YAML = os.path.join(REPO_ROOT, "nodes.yaml")

POLL_INTERVAL = 5
REQUEST_TIMEOUT = 3
COLLECTOR_PORT = 9100
COLLECTOR_PATH = "/metrics"

# GitHub OAuth
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
CLUSTERPULSE_HOSTNAME = os.environ.get("CLUSTERPULSE_HOSTNAME", "monitor.ecoseek.org")
ALLOWED_USERS = set(
    u.strip() for u in os.environ.get("ALLOWED_USERS", "alrobles").split(",") if u.strip()
)
GITHUB_OAUTH_URL = "https://github.com/login/oauth"
SESSION_SECRET_KEY = os.environ.get("SESSION_SECRET_KEY", secrets.token_urlsafe(64))

AUTH_ENABLED = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)

# ---------------------------------------------------------------------------
# Load node metadata
# ---------------------------------------------------------------------------
with open(NODES_YAML) as f:
    _data = yaml.safe_load(f)

NODES = _data["nodes"]

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache: dict = {
    "nodes": {name: {"error": "not_yet_polled"} for name in NODES},
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "poll_interval_seconds": POLL_INTERVAL,
}

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _auth_required(request: Request):
    """Raise 401 if auth is enabled and user is not logged in."""
    if not AUTH_ENABLED:
        return
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get("login") not in ALLOWED_USERS:
        raise HTTPException(status_code=403, detail="User not authorized")


def _get_redirect_uri(request: Request) -> str:
    """Build the OAuth redirect URI from the request."""
    scheme = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", CLUSTERPULSE_HOSTNAME)
    return f"{scheme}://{host}/auth/callback"


# ---------------------------------------------------------------------------
# Polling logic
# ---------------------------------------------------------------------------

async def _poll_node(name: str, ip: str) -> tuple[str, dict]:
    url = f"http://{ip}:{COLLECTOR_PORT}{COLLECTOR_PATH}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return name, resp.json()
    except Exception:
        return name, {"error": "unreachable"}


async def _poll_all() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    tasks = [_poll_node(name, meta["tailscale_ip"]) for name, meta in NODES.items()]
    results = await asyncio.gather(*tasks)
    node_results = {name: metrics for name, metrics in results}
    return {
        "nodes": node_results,
        "timestamp": now,
        "poll_interval_seconds": POLL_INTERVAL,
    }


async def _polling_loop():
    while True:
        try:
            _cache.update(await _poll_all())
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_polling_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ClusterPulse", version="0.2.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, max_age=86400)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def auth_login(request: Request):
    """Redirect to GitHub OAuth."""
    if not AUTH_ENABLED:
        return RedirectResponse(url="/")
    redirect_uri = _get_redirect_uri(request)
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    params = httpx.QueryParams({
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user",
        "state": state,
    })
    url = f"{GITHUB_OAUTH_URL}/authorize?{params}"
    return RedirectResponse(url=url)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    """Handle GitHub OAuth callback."""
    if not AUTH_ENABLED:
        return RedirectResponse(url="/")

    # Validate state
    saved_state = request.session.pop("oauth_state", None)
    if not saved_state or saved_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    # Exchange code for access token
    token_url = f"{GITHUB_OAUTH_URL}/access_token"
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            token_url,
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": _get_redirect_uri(request),
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail=f"OAuth failed: {token_data}")

        # Get user info
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_data = user_resp.json()

    # Store in session
    request.session["user"] = {
        "login": user_data["login"],
        "name": user_data.get("name", user_data["login"]),
        "avatar_url": user_data.get("avatar_url", ""),
        "id": user_data["id"],
    }

    return RedirectResponse(url="/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/")


@app.get("/auth/me")
async def auth_me(request: Request):
    """Return current user info or 401."""
    user = request.session.get("user")
    if not user:
        return JSONResponse({"authenticated": False})
    return {
        "authenticated": True,
        "login": user["login"],
        "name": user["name"],
        "avatar_url": user["avatar_url"],
        "allowed_users": sorted(ALLOWED_USERS),
    }


# ---------------------------------------------------------------------------
# Protected API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status(request: Request):
    """Return cached cluster-wide status (requires auth)."""
    _auth_required(request)
    return _cache


# ---------------------------------------------------------------------------
# Protected dashboard
# ---------------------------------------------------------------------------

@app.get("/")
async def dashboard(request: Request):
    """Serve the dashboard (only if authenticated)."""
    _auth_required(request)
    # Fall through to static files
    return Response(status_code=200)


# Serve static files (dashboard is protected by the / route above)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
