"""
WordComet — Auth & Profile
Endpoints:
    POST   /auth/signup         — register new user (Firebase token + role required)
    POST   /auth/login          — login / sync user info from Firebase
    GET    /profile/me          — get own full profile
    PATCH  /profile/me          — update own profile fields
    GET    /profile/{uid}       — view another user's public profile
"""

import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from firebase_admin import auth as fb_auth

load_dotenv()

router = APIRouter()
bearer_scheme = HTTPBearer()

# ─── DB Connection ───────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(
        dbname=os.environ.get("DB_NAME", "wordcomet"),
        user=os.environ.get("DB_USER", "wordcomet_user"),
        password=os.environ.get("DB_PASSWORD", ""),
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=os.environ.get("DB_PORT", "5432"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


# ─── Auth Helper ─────────────────────────────────────────────────────

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    """Verify Firebase ID token and return decoded payload."""
    try:
        decoded = fb_auth.verify_id_token(credentials.credentials)
        return decoded
    except fb_auth.InvalidIdTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Unauthorized: {str(e)}")


# ─── Pydantic Models ─────────────────────────────────────────────────

VALID_ROLES = ("mentor", "mentee")

class SignupBody(BaseModel):
    id_token: str
    role: str                               # "mentor" or "mentee"
    display_name: str
    full_name: Optional[str] = None

class LoginBody(BaseModel):
    id_token: str
    fcm_token: Optional[str] = None
    device_id: Optional[str] = None

class UpdateProfileBody(BaseModel):
    display_name: Optional[str] = None
    full_name: Optional[str] = None
    profile_picture_url: Optional[str] = None
    preferred_language: Optional[str] = None
    timezone: Optional[str] = None
    notification_time: Optional[str] = None  # "HH:MM" format
    notifications_enabled: Optional[bool] = None


# ─── 1. Signup ───────────────────────────────────────────────────────

@router.post("/auth/signup", status_code=201)
def signup(body: SignupBody):
    """
    Register a new user. Role is set here and cannot be changed later.

    Headers: none (token is in body for signup)
    Body:
    {
        "id_token": "<firebase_id_token>",
        "role": "mentor",             ← or "mentee"
        "display_name": "John",
        "full_name": "John Doe"       ← optional
    }
    """
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of: {VALID_ROLES}")

    if not body.display_name or not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name is required.")

    # Verify Firebase token
    try:
        decoded = fb_auth.verify_id_token(body.id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase token.")

    firebase_uid = decoded["uid"]
    email = decoded.get("email")
    picture = decoded.get("picture")

    if not email:
        raise HTTPException(status_code=400, detail="Google account must have an email.")

    conn = get_db()
    try:
        cur = conn.cursor()

        # Check if already registered
        cur.execute("SELECT id, role FROM users WHERE firebase_uid = %s", (firebase_uid,))
        existing = cur.fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="User already exists. Use /auth/login instead.")

        # Insert new user
        cur.execute("""
            INSERT INTO users (
                firebase_uid, email, display_name, full_name,
                profile_picture_url, role, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
            RETURNING id, firebase_uid, email, display_name, full_name,
                      profile_picture_url, role, created_at
        """, (
            firebase_uid,
            email,
            body.display_name.strip(),
            body.full_name.strip() if body.full_name else None,
            picture,
            body.role,
        ))

        user = dict(cur.fetchone())
        conn.commit()
        return {"status": "created", "user": user}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Signup failed: {str(e)}")
    finally:
        conn.close()


# ─── 2. Login ────────────────────────────────────────────────────────

@router.post("/auth/login")
def login(body: LoginBody):
    """
    Login — verifies Firebase token, syncs latest info, updates last_active.
    Call this every time the app opens.

    Body:
    {
        "id_token": "<firebase_id_token>",
        "fcm_token": "...",    ← optional, updates push token
        "device_id": "..."     ← optional
    }
    """
    try:
        decoded = fb_auth.verify_id_token(body.id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase token.")

    firebase_uid = decoded["uid"]
    email = decoded.get("email")
    picture = decoded.get("picture")

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT * FROM users WHERE firebase_uid = %s", (firebase_uid,))
        user = cur.fetchone()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="User not registered. Please sign up first."
            )

        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="Account is deactivated.")

        # Sync latest Firebase info + update last_active
        cur.execute("""
            UPDATE users SET
                email = %s,
                profile_picture_url = COALESCE(%s, profile_picture_url),
                fcm_token = COALESCE(%s, fcm_token),
                device_id = COALESCE(%s, device_id),
                last_active_at = NOW(),
                updated_at = NOW()
            WHERE firebase_uid = %s
            RETURNING *
        """, (
            email,
            picture,
            body.fcm_token,
            body.device_id,
            firebase_uid,
        ))

        updated_user = dict(cur.fetchone())
        conn.commit()

        # Remove sensitive fields before returning
        updated_user.pop("fcm_token", None)
        updated_user.pop("device_id", None)

        return {"status": "ok", "user": updated_user}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Login failed: {str(e)}")
    finally:
        conn.close()


# ─── 3. Get Own Profile ──────────────────────────────────────────────

@router.get("/profile/me")
def get_my_profile(current_user: dict = Depends(get_current_user)):
    """
    Get full profile of the logged-in user.
    Headers: Authorization: Bearer <firebase_id_token>
    """
    firebase_uid = current_user["uid"]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE firebase_uid = %s", (firebase_uid,))
        user = cur.fetchone()

        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        user = dict(user)
        # Remove internal fields
        user.pop("fcm_token", None)
        user.pop("device_id", None)

        return user

    finally:
        conn.close()


# ─── 4. Update Own Profile ───────────────────────────────────────────

@router.patch("/profile/me")
def update_my_profile(
    body: UpdateProfileBody,
    current_user: dict = Depends(get_current_user)
):
    """
    Update editable profile fields. Role cannot be changed after signup.
    Headers: Authorization: Bearer <firebase_id_token>

    Body (all fields optional):
    {
        "display_name": "Johnny",
        "full_name": "John Doe",
        "profile_picture_url": "https://...",
        "preferred_language": "en",
        "timezone": "Asia/Kolkata",
        "notification_time": "08:00",
        "notifications_enabled": true
    }
    """
    firebase_uid = current_user["uid"]

    # Build dynamic SET clause from only provided fields
    fields = {}
    if body.display_name is not None:
        if not body.display_name.strip():
            raise HTTPException(status_code=400, detail="display_name cannot be empty.")
        fields["display_name"] = body.display_name.strip()
    if body.full_name is not None:
        fields["full_name"] = body.full_name.strip()
    if body.profile_picture_url is not None:
        fields["profile_picture_url"] = body.profile_picture_url
    if body.preferred_language is not None:
        fields["preferred_language"] = body.preferred_language
    if body.timezone is not None:
        fields["timezone"] = body.timezone
    if body.notification_time is not None:
        fields["notification_time"] = body.notification_time
    if body.notifications_enabled is not None:
        fields["notifications_enabled"] = body.notifications_enabled

    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    fields["updated_at"] = datetime.now(timezone.utc)

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [firebase_uid]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE users SET {set_clause} WHERE firebase_uid = %s RETURNING *",
            values
        )
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="User not found.")

        conn.commit()
        updated = dict(updated)
        updated.pop("fcm_token", None)
        updated.pop("device_id", None)

        return {"status": "updated", "user": updated}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")
    finally:
        conn.close()


# ─── 5. View Another User's Public Profile ───────────────────────────

@router.get("/profile/{firebase_uid}")
def get_public_profile(
    firebase_uid: str,
    current_user: dict = Depends(get_current_user)
):
    """
    View another user's public profile.
    Headers: Authorization: Bearer <firebase_id_token>
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                firebase_uid,
                display_name,
                full_name,
                profile_picture_url,
                role,
                preferred_language,
                created_at,
                last_active_at
            FROM users
            WHERE firebase_uid = %s AND is_active = TRUE
        """, (firebase_uid,))

        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        return dict(user)

    finally:
        conn.close()

@app.get("/status")
async def server_status():
    return {
        "teachers_available": len(waiting_teachers),
        "students_waiting":   len(waiting_students),
        "active_sessions":    len(active_rooms),
    }