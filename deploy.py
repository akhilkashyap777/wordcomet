"""
WordComet — Word of the Day Server
Single file. No CLI. Push words via POST /admin/set-word.
Word persists to current_word.json (survives restarts).
"""

import uvicorn
import os
import json
from datetime import date
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException

load_dotenv()

import firebase_admin
from firebase_admin import credentials, messaging


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_JSON = os.path.join(BASE_DIR, "wordcomet.json")
TOKEN_STORE_FILE = os.path.join(BASE_DIR, "fcm_tokens.json")
WORD_STORE_FILE = os.path.join(BASE_DIR, "current_word.json")

ADMIN_API_KEY = os.environ.get("WORDCOMET_ADMIN_KEY")

# ─── Firebase Init ───────────────────────────────────────────────────

if os.path.exists(SERVICE_ACCOUNT_JSON):
    cred = credentials.Certificate(SERVICE_ACCOUNT_JSON)
    firebase_admin.initialize_app(cred)
    print("🔥 Firebase initialized successfully.")
else:
    print(f"⚠️  Firebase key not found at {SERVICE_ACCOUNT_JSON}")
    print("   Push notifications will NOT work.")

# ─── FCM Token Storage ──────────────────────────────────────────────

def _load_tokens() -> set:
    if os.path.exists(TOKEN_STORE_FILE):
        with open(TOKEN_STORE_FILE, "r") as f:
            return set(json.load(f))
    return set()

def _save_tokens(tokens: set):
    with open(TOKEN_STORE_FILE, "w") as f:
        json.dump(list(tokens), f)

fcm_tokens: set = _load_tokens()

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

app = FastAPI(title="WordComet API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    send_notification: Optional[bool] = True

# ─── Public Endpoints ───────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "WordComet",
        "status": "running",
        "has_word": current_word is not None,
    }


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
def register_device(body: TokenRegister):
    """Extension/app registers for push notifications."""
    if not body.token:
        raise HTTPException(status_code=400, detail="Token cannot be empty.")
    fcm_tokens.add(body.token)
    _save_tokens(fcm_tokens)
    return {"status": "registered", "total_devices": len(fcm_tokens)}


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
        "date": str(date.today()),
    }

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