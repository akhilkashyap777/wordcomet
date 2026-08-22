"""
WordComet — Word of the Day Server
Single file. No CLI. Push words via POST /admin/set-word.
Word persists to current_word.json (survives restarts).
"""

import uvicorn
import os
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi import UploadFile, File, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import shutil
import httpx
import uuid
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from notification_store import (
    fcm_tokens,
    fcm_countries,
    _save_tokens,
    _save_countries,
)
import geoip2.database

# from database import db_session

load_dotenv()
#load_dotenv("/var/www/wordcomet/.env")

from announcements import router as announcements_router

import firebase_admin
from firebase_admin import credentials, messaging

from auth import router as auth_router
from quiz import router as quiz_router
from friends import router as friends_router, db_session
from challenges import router as challenges_router
from websocket_routes import router as websocket_router
from mentor_slots import router as mentor_slots_router


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GEOIP_DATABASE = os.path.join(BASE_DIR, "GeoLite2-Country.mmdb")
geoip_reader = (
    geoip2.database.Reader(GEOIP_DATABASE)
    if os.path.exists(GEOIP_DATABASE)
    else None
)

# live credentials
SERVICE_ACCOUNT_JSON = os.path.join(BASE_DIR, "wordcomet.json")

WORD_IMAGE_DIR = os.path.join(BASE_DIR, "static", "word_images")
os.makedirs(WORD_IMAGE_DIR, exist_ok=True)

# test firebase credentials
# SERVICE_ACCOUNT_JSON = os.path.join(BASE_DIR, "wordcomet-testenv.json")

# TOKEN_STORE_FILE = os.path.join(BASE_DIR, "fcm_tokens.json")
WORD_STORE_FILE = os.path.join(BASE_DIR, "current_word.json")


ADMIN_API_KEY = os.environ.get("WORDCOMET_ADMIN_KEY")

TURN_TOKEN_ID = os.environ.get("TURN_TOKEN_ID")
TURN_API_TOKEN = os.environ.get("TURN_API_TOKEN")

# ─── Firebase Init ───────────────────────────────────────────────────

if os.path.exists(SERVICE_ACCOUNT_JSON):
    cred = credentials.Certificate(SERVICE_ACCOUNT_JSON)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    print("Firebase project loaded:", firebase_admin.get_app().project_id)
    print("🔥 Firebase initialized successfully.")
else:
    print(f"⚠️  Firebase key not found at {SERVICE_ACCOUNT_JSON}")
    print("   Push notifications will NOT work.")

# ─── FCM Token Storage ──────────────────────────────────────────────

# def _load_tokens() -> set:
#     if os.path.exists(TOKEN_STORE_FILE):
#         with open(TOKEN_STORE_FILE, "r") as f:
#             return set(json.load(f))
#     return set()

# def _save_tokens(tokens: set):
#     with open(TOKEN_STORE_FILE, "w") as f:
#         json.dump(list(tokens), f)

# fcm_tokens: set = _load_tokens()

# ─── Word Persistence ───────────────────────────────────────────────

def _load_word() -> dict | None:
    if os.path.exists(WORD_STORE_FILE):
        with open(WORD_STORE_FILE, "r") as f:
            return json.load(f)
    return None

def _save_word(word_data: dict):
    with open(WORD_STORE_FILE, "w") as f:
        json.dump(word_data, f, indent=2)

current_word: dict | None = _load_word()

# ─── App Setup ───────────────────────────────────────────────────────

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # This block runs BEFORE the server starts up (Startup)
#     await db_session.connect()
#     yield

app = FastAPI(title="WordComet API")
# app = FastAPI(title="WordComet API", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(friends_router)
app.include_router(challenges_router)
app.include_router(websocket_router)
app.include_router(mentor_slots_router)
app.include_router(quiz_router)
app.include_router(announcements_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#__ database 500 fix for firends search _______

@app.on_event("startup")
async def startup():
    await db_session.connect()
    print("✅ Friends DB pool connected")

@app.on_event("shutdown")
async def shutdown():
    if db_session.pool:
        await db_session.pool.close()
        print("✅ Friends DB pool closed")

@app.on_event("shutdown")
async def shutdown():
    await db_session.disconnect()

# ─── Auth Helper ─────────────────────────────────────────────────────

def verify_admin(x_api_key: str):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key.")

# ─── Pydantic Models ────────────────────────────────────────────────

class TokenRegister(BaseModel):
    token: str

class WordInput(BaseModel):
    word: str
    meaning: str
    part_of_speech: str
    example: Optional[str] = None
    pronunciation: Optional[str] = None
    phonetics: Optional[str] = None
    language: Optional[str] = "English"

    synonyms: Optional[list[str]] = None
    antonyms: Optional[list[str]] = None

    base_form: Optional[str] = None
    past_form: Optional[str] = None
    past_participle: Optional[str] = None
    present_participle: Optional[str] = None
    third_person_singular: Optional[str] = None

    comparative_form: Optional[str] = None
    superlative_form: Optional[str] = None

    send_notification: Optional[bool] = True

# ─── Public Endpoints ───────────────────────────────────────────────

# @app.get("/")
# def root():
#     return {
#         "service": "WordComet",
#         "status": "running",
#         "has_word": current_word is not None,
#     }

@app.get("/")
def root():
    return FileResponse(os.path.join(BASE_DIR, "static", "login.html"))

@app.get("/word-of-the-day")
def get_word():
    """Full word with all details."""
    if not current_word:
        raise HTTPException(status_code=404, detail="No word set for today yet.")
    return current_word


@app.get("/word-of-the-day/brief")
def get_word_brief():
    """Just word + meaning + part_of_speech + date."""
    if not current_word:
        raise HTTPException(status_code=404, detail="No word set for today yet.")
    return {
        "word": current_word["word"],
        "meaning": current_word["meaning"],
        "part_of_speech": current_word["part_of_speech"],
        "date": current_word["date"],
    }


@app.post("/register-device")
def register_device(body: TokenRegister, request: Request):
    """Register an FCM token and detect its country."""

    if not body.token:
        raise HTTPException(status_code=400, detail="Token cannot be empty.")

    forwarded_for = request.headers.get("x-forwarded-for")

    if forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.client.host

    country = "UNKNOWN"

    try:
        country_response = geoip_reader.country(client_ip)
        country = country_response.country.iso_code or "UNKNOWN"
    except Exception as error:
        print(f"Country detection failed for {client_ip}: {error}")

    fcm_tokens.add(body.token)
    fcm_countries[body.token] = country

    _save_tokens(fcm_tokens)
    _save_countries()

    return {
        "status": "registered",
        "country": country,
        "total_devices": len(fcm_tokens)
    }

@app.delete("/unregister-device")
def unregister_device(body: TokenRegister):
    """Remove a device token."""
    fcm_tokens.discard(body.token)
    _save_tokens(fcm_tokens)
    return {"status": "unregistered", "total_devices": len(fcm_tokens)}


# ─── Admin Endpoint ─────────────────────────────────────────────────

@app.post("/admin/set-word")
def set_word(
    body: WordInput,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(...)
):
    """
    Set today's word. Overwrites any existing word.
    Persists to file. Optionally sends push notification.

    Headers:
        x-api-key: your-secret-key

    Body:
    {
        "word": "Ephemeral",
        "meaning": "Lasting for a very short time",
        "part_of_speech": "Adjective",
        "example": "The ephemeral beauty of cherry blossoms.",
        "pronunciation": "ih-FEM-er-uhl",
        "phonetics": "/ɪˈfɛm.ər.əl/",
        "language": "English (from Greek)",
        "send_notification": true
    }
    """
    global current_word
    verify_admin(x_api_key)

    current_word = {
        "word": body.word,
        "meaning": body.meaning,
        "part_of_speech": body.part_of_speech,
        "example": body.example,
        "pronunciation": body.pronunciation,
        "phonetics": body.phonetics,
        "language": body.language or "English",
        "date": datetime.now().isoformat(timespec="seconds"),
    }

    current_word = {
    "word": body.word,
    "meaning": body.meaning,
    "part_of_speech": body.part_of_speech,
    "example": body.example,
    "pronunciation": body.pronunciation,
    "phonetics": body.phonetics,
    "language": body.language or "English",
    "date": datetime.now().isoformat(timespec="seconds"),
    }

    if body.synonyms:
        current_word["synonyms"] = body.synonyms

    if body.antonyms:
        current_word["antonyms"] = body.antonyms

    if body.base_form:
        current_word["base_form"] = body.base_form

    if body.past_form:
        current_word["past_form"] = body.past_form

    if body.past_participle:
        current_word["past_participle"] = body.past_participle

    if body.present_participle:
        current_word["present_participle"] = body.present_participle

    if body.third_person_singular:
        current_word["third_person_singular"] = body.third_person_singular

    if body.comparative_form:
        current_word["comparative_form"] = body.comparative_form

    if body.superlative_form:
        current_word["superlative_form"] = body.superlative_form

    _save_word(current_word)

    if body.send_notification:
        background_tasks.add_task(send_push_notification, current_word)

    return {
        "status": "success",
        "message": f"'{body.word}' is now live!",
        "word": current_word,
    }


# ─── Push Notification Helper ───────────────────────────────────────

def send_push_notification(word_data: dict):
    """Send FCM notification to all registered devices."""
    if not firebase_admin._apps:
        print("⚠️  Firebase not initialized — skipping push.")
        return

    if not fcm_tokens:
        print("📭 No registered devices — skipping push.")
        return

    stale = set()
    success_count = 0

    for token in list(fcm_tokens):
        message = messaging.Message(
            notification=messaging.Notification(
                title=f"☄️ Word of the Day: {word_data['word']}",
                body=word_data["meaning"],
            ),
            data={
                "type": "wod",
                "word": word_data["word"],
                "meaning": word_data["meaning"],
                "part_of_speech": word_data["part_of_speech"],
                "example": word_data.get("example") or "",
                "pronunciation": word_data.get("pronunciation") or "",
                "phonetics": word_data.get("phonetics") or "",
                "language": word_data.get("language") or "English",
                "date": word_data["date"],
            },
            token=token,
        )
        try:
            messaging.send(message)
            success_count += 1
        except messaging.UnregisteredError:
            stale.add(token)
        except Exception as e:
            print(f"   ⚠️  Failed for a token: {e}")

    if stale:
        fcm_tokens.difference_update(stale)
        _save_tokens(fcm_tokens)
        print(f"   🧹 Removed {len(stale)} stale token(s).")

    print(f"   📬 Sent to {success_count}/{len(fcm_tokens) + len(stale)} device(s).")

@app.get("/admin/devices")
def get_devices(x_api_key: str = Header(...)):
    verify_admin(x_api_key)
    return {"count": len(fcm_tokens)}


# ______ static files _________________________________________________

app.mount("/", StaticFiles(directory="static", html=True), name="static")

# ─── Run ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  WordComet API — Running!")
    print("  API:  http://0.0.0.0:8011")
    print("  Docs: http://127.0.0.1:8011/docs")
    if current_word:
        print(f"  Word: {current_word['word']} ({current_word['date']})")
    else:
        print("  Word: None set yet")
    print("=" * 50 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8011, log_level="info")
