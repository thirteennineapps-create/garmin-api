from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
import asyncio
import hashlib
import os
import datetime
import logging
import queue
import re
import secrets
import shutil
import threading
import time
import uuid
from typing import Optional
from garth.exc import GarthHTTPError
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# Initialize the FastAPI app
app = FastAPI()

# Allow the local React dev app (running-plan-app, e.g. Vite on
# localhost:5173) to call this API from the browser. Restricted to
# localhost origins since this API holds a personal Garmin session.
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

_DEFAULT_ALLOWED_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
# Comma-separated override for hosting this somewhere the Vite dev server
# doesn't live - e.g. the origin a packaged Capacitor Android build loads
# from (typically "https://localhost"). Leave unset for today's local setup.
_origins_env = os.getenv("ALLOWED_ORIGINS")
ALLOWED_ORIGINS = (
    [o.strip() for o in _origins_env.split(",") if o.strip()]
    if _origins_env
    else _DEFAULT_ALLOWED_ORIGINS
)

@app.middleware("http")
async def enforce_api_key(request, call_next):
    """When API_KEY is set (i.e. this instance is reachable beyond your own
    machine/private network), every route except the "/" health check must
    present a matching X-API-Key header. CORS preflight (OPTIONS) is exempt
    so browsers can still complete the preflight handshake."""
    if API_KEY and request.method != "OPTIONS" and request.url.path != "/":
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key header."})
    return await call_next(request)

# CORS must be added AFTER enforce_api_key: Starlette treats the last-added
# middleware as the outermost one, so this way even the 401 above carries CORS
# headers. Added first, a missing/wrong key made the browser (or Android
# WebView) drop the 401 as an opaque network error, so the app only said
# "can't reach the API" instead of showing the real problem.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables if defined
TOKENSTORE = os.getenv("GARMINTOKENS") or "~/.garminconnect"

# Belt-and-suspenders: garth.dump() already creates this directory itself,
# but make sure it exists up front too.
os.makedirs(os.path.expanduser(TOKENSTORE), exist_ok=True)


def _fresh_login(garmin: "Garmin") -> None:
    """Force a real Garmin SSO login with the email/password already set on
    `garmin`, bypassing garminconnect's `Garmin.login()` auto-pickup of the
    GARMINTOKENS env var.

    `Garmin.login(tokenstore=None)` does `tokenstore = tokenstore or
    os.getenv("GARMINTOKENS")` - so whenever GARMINTOKENS is set in the
    process environment (as it is on Fly, via fly.toml's [env] block), a
    bare `garmin.login()` call *always* tries to load a saved session from
    that directory instead of doing a fresh network login, even though the
    caller's whole point was "no saved session yet, log in with credentials".
    On a brand-new deploy with an empty volume that raises FileNotFoundError
    looking for oauth1_token.json, because there is nothing to load yet.
    Locally this never showed up, since GARMINTOKENS isn't exported as an
    actual environment variable there (only main.py's own TOKENSTORE
    fallback is), so the bug is Fly-only.

    Temporarily hiding the env var for the duration of this one call sidesteps
    it without touching garminconnect/garth themselves.
    """
    saved = os.environ.pop("GARMINTOKENS", None)
    try:
        garmin.login()
    finally:
        if saved is not None:
            os.environ["GARMINTOKENS"] = saved


# Optional shared-secret gate. Unset (the default local/dev setup) means every
# route below stays exactly as open as it is today - localhost only, no key
# needed. Set API_KEY before hosting this anywhere reachable off your own
# machine/private network (see README): every route except "/" then requires
# a matching `X-API-Key` header, since none of the get_* endpoints otherwise
# check who is asking.
API_KEY = os.getenv("API_KEY")

# Define the date range
today = datetime.date.today()
startdate = today - datetime.timedelta(days=7)

# ---------------------------------------------------------------------------
# Per-user sessions
#
# Every user logs in with their own Garmin account. A successful login mints a
# random session id (256 bits) that the app keeps and sends on each request as
# `X-Session-Id`. That user's Garmin tokens are stored in their own directory
# under TOKENSTORE/sessions/, named by a SHA-256 of the id so that listing the
# directory on disk never reveals a usable id. Garmin passwords are never
# stored - only the OAuth tokens garth issues.
# ---------------------------------------------------------------------------
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SESSIONS_DIR = os.path.join(os.path.expanduser(TOKENSTORE), "sessions")

# Hosted setups (GARMINTOKENS set, e.g. on Fly) used to keep ONE shared login
# at the top of TOKENSTORE. Nothing reads it any more, so remove the stale
# tokens. Left alone when TOKENSTORE is just the default ~/.garminconnect,
# which other garth-based tools on a dev machine may still use.
if os.getenv("GARMINTOKENS"):
    for _legacy in ("oauth1_token.json", "oauth2_token.json"):
        try:
            os.remove(os.path.join(os.path.expanduser(TOKENSTORE), _legacy))
        except FileNotFoundError:
            pass


def _new_session_id() -> str:
    return secrets.token_urlsafe(32)


def _valid_session_id(session_id: Optional[str]) -> bool:
    return bool(session_id) and bool(_SESSION_ID_RE.match(session_id))


def _session_dir(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, hashlib.sha256(session_id.encode()).hexdigest())


def _http_status(err: Exception) -> Optional[int]:
    resp = getattr(getattr(err, "error", None), "response", None)
    return getattr(resp, "status_code", None)


def _load_session(session_id: Optional[str]) -> "Garmin":
    """Return a logged-in Garmin client for this session id, or raise 401.
    The 401 details start with 'Not logged in' / 'Garmin session expired' so
    the app can tell them apart from a bad X-API-Key."""
    if not _valid_session_id(session_id) or not os.path.isdir(_session_dir(session_id)):
        raise HTTPException(status_code=401, detail="Not logged in to Garmin.")
    try:
        garmin = Garmin()
        garmin.login(_session_dir(session_id))
    except (FileNotFoundError, GarminConnectAuthenticationError):
        raise HTTPException(status_code=401, detail="Garmin session expired - please log in again.")
    except GarthHTTPError as err:
        if _http_status(err) in (401, 403):
            raise HTTPException(status_code=401, detail="Garmin session expired - please log in again.")
        logger.error(err)
        raise HTTPException(status_code=502, detail="Garmin Connect returned an error. Try again shortly.")
    except GarminConnectConnectionError as err:
        logger.error(err)
        raise HTTPException(status_code=502, detail="Could not reach Garmin Connect. Try again shortly.")
    return garmin


# Dependency: the Garmin client for whoever sent X-Session-Id
def get_garmin_api(x_session_id: Optional[str] = Header(default=None)):
    return _load_session(x_session_id)


# ---------------------------------------------------------------------------
# Login module
#
# One-time login flow so a Garmin email/password only ever needs to be
# entered once (from the React app's login screen), after which the saved
# token in TOKENSTORE lets get_garmin_api() above authenticate silently on
# every later request - no credentials kept in memory or sent again.
#
# Garmin Connect commonly requires an MFA code. The garth login() call
# resolves MFA *synchronously* inside a single blocking call via a
# `prompt_mfa` callback - there is no built-in "pause and resume later"
# API in this version of the library. To expose that over two separate
# HTTP requests (submit credentials -> show an MFA field -> submit the
# code), the login runs in a background thread and the callback blocks on
# a queue that the second request pushes the code into. Pending logins live
# in a small in-memory dict keyed by a login id - no database needed.
# ---------------------------------------------------------------------------

_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# login_id -> {"mfa_queue": Queue, "prompted": Event, "done": Event,
#              "success": bool | None, "error": str | None}
_pending_logins: dict[str, dict] = {}
_pending_logins_lock = threading.Lock()

MFA_TIMEOUT_SECONDS = 300  # how long we'll wait for the MFA code to arrive
LOGIN_POLL_TIMEOUT_SECONDS = 20  # how long /login waits before assuming MFA is needed


class LoginRequest(BaseModel):
    email: str
    password: str


class MfaRequest(BaseModel):
    login_id: str
    code: str


def _run_login(login_id: str, email: str, password: str) -> None:
    """Runs on a background thread; fills in the shared state dict as it goes."""
    state = _pending_logins[login_id]

    def prompt_mfa() -> str:
        state["prompted"].set()
        code = state["mfa_queue"].get(timeout=MFA_TIMEOUT_SECONDS)  # may raise queue.Empty
        return code

    try:
        garmin = Garmin(email=email, password=password, is_cn=False, prompt_mfa=prompt_mfa)
        garmin.garth.sess.headers.update({"User-Agent": _DESKTOP_USER_AGENT})
        _fresh_login(garmin)
        garmin.garth.dump(_session_dir(state["session_id"]))
        state["success"] = True
    except queue.Empty:
        state["success"] = False
        state["error"] = "No MFA code was entered in time."
    except (GarminConnectAuthenticationError, GarthHTTPError) as err:
        state["success"] = False
        state["error"] = "Garmin rejected that email/password (or MFA code)."
        logger.info("Garmin login failed: %s", err)
    except Exception as err:  # noqa: BLE001 - surfaced to the (local, single-user) caller
        state["success"] = False
        state["error"] = f"Login failed: {err}"
        logger.exception("Unexpected error during Garmin login")
    finally:
        state["done"].set()


@app.post("/login")
async def login(payload: LoginRequest):
    """Start a Garmin login. Returns immediately with either success, an
    error, or {"status": "mfa_required", "login_id": ...} if Garmin wants a
    verification code - in that case, call POST /login/mfa next."""
    login_id = str(uuid.uuid4())
    state = {
        "mfa_queue": queue.Queue(maxsize=1),
        "prompted": threading.Event(),
        "done": threading.Event(),
        "success": None,
        "error": None,
        "session_id": _new_session_id(),
    }
    with _pending_logins_lock:
        _pending_logins[login_id] = state

    thread = threading.Thread(
        target=_run_login, args=(login_id, payload.email, payload.password), daemon=True
    )
    thread.start()

    # Wait for whichever happens first: login finishes outright (no MFA
    # needed, or a fast credential failure), or garth asks for an MFA code.
    # (Polling both events - waiting on just "prompted" made every non-MFA login
    # sit out the whole timeout even though it had already finished.)
    deadline = time.monotonic() + LOGIN_POLL_TIMEOUT_SECONDS
    while not (state["done"].is_set() or state["prompted"].is_set()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    done = state["done"].is_set()
    mfa_needed = state["prompted"].is_set()

    if not done and mfa_needed:
        return {"status": "mfa_required", "login_id": login_id}

    if not done:
        # Neither happened within the timeout - something is stuck upstream.
        raise HTTPException(status_code=504, detail="Login timed out talking to Garmin Connect.")

    with _pending_logins_lock:
        _pending_logins.pop(login_id, None)
    if state["success"]:
        return {"status": "ok", "session_id": state["session_id"]}
    raise HTTPException(status_code=401, detail=state["error"] or "Login failed.")


@app.post("/login/mfa")
async def login_mfa(payload: MfaRequest):
    """Submit the MFA code for a login started via POST /login."""
    with _pending_logins_lock:
        state = _pending_logins.get(payload.login_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Unknown or expired login attempt - start over with /login.")

    state["mfa_queue"].put(payload.code)
    finished = state["done"].wait(timeout=LOGIN_POLL_TIMEOUT_SECONDS)

    with _pending_logins_lock:
        _pending_logins.pop(payload.login_id, None)

    if not finished:
        raise HTTPException(status_code=504, detail="Timed out waiting for Garmin to verify the MFA code.")
    if state["success"]:
        return {"status": "ok", "session_id": state["session_id"]}
    raise HTTPException(status_code=401, detail=state["error"] or "MFA verification failed.")


@app.get("/auth_status")
async def auth_status(x_session_id: Optional[str] = Header(default=None)):
    """Cheap check for whether this session (X-Session-Id) still has a working
    Garmin login, so the app knows whether to show the login screen."""
    try:
        garmin = _load_session(x_session_id)
    except HTTPException:
        return {"authenticated": False}
    except Exception:  # noqa: BLE001 - treat anything unexpected as "not logged in"
        return {"authenticated": False}
    return {"authenticated": True, "full_name": garmin.get_full_name()}


@app.post("/logout")
async def logout(x_session_id: Optional[str] = Header(default=None)):
    """Delete this session's saved Garmin tokens (other users are untouched)."""
    if _valid_session_id(x_session_id):
        shutil.rmtree(_session_dir(x_session_id), ignore_errors=True)
    return {"status": "ok"}


@app.get("/get_full_name")
async def get_full_name(api: Garmin = Depends(get_garmin_api)):
    return {"full_name": api.get_full_name()}

@app.get("/get_unit_system")
async def get_unit_system(api: Garmin = Depends(get_garmin_api)):
    return {"unit_system": api.get_unit_system()}

@app.get("/get_activity_data")
async def get_activity_data(date: str = today.isoformat(), api: Garmin = Depends(get_garmin_api)):
    return {"activity_data": api.get_stats(date)}

@app.get("/get_body_composition")
async def get_body_composition(date: str = today.isoformat(), api: Garmin = Depends(get_garmin_api)):
    return {"body_composition": api.get_body_composition(date)}

@app.get("/get_steps_data")
async def get_steps_data(date: str = today.isoformat(), api: Garmin = Depends(get_garmin_api)):
    return {"steps_data": api.get_steps_data(date)}

@app.get("/get_heart_rate_data")
async def get_heart_rate_data(date: str = today.isoformat(), api: Garmin = Depends(get_garmin_api)):
    return {"heart_rate_data": api.get_heart_rates(date)}

@app.get("/get_training_readiness")
async def get_training_readiness(date: str = today.isoformat(), api: Garmin = Depends(get_garmin_api)):
    return {"training_readiness": api.get_training_readiness(date)}

@app.get("/get_activities")
async def get_activities(start: int = 0, limit: int = 100, api: Garmin = Depends(get_garmin_api)):
    return {"activities": api.get_activities(start, limit)}

@app.get("/get_last_activity")
async def get_last_activity(api: Garmin = Depends(get_garmin_api)):
    return {"last_activity": api.get_last_activity()}


@app.get("/get_previous_day_steps")
async def get_previous_day_steps(date: str = None, api: Garmin = Depends(get_garmin_api)):
    if date is None:
        date = (today - datetime.timedelta(days=1)).isoformat()
    steps_data = api.get_daily_steps(date, date)
    return {"previous_day_steps": steps_data}


# Add more routes here for other functionalities...

@app.get("/")
async def root():
    return {"message": "Garmin Connect API via FastAPI"}
