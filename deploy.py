"""
Word of the Day — Single File
Runs the FastAPI server AND gives you a CLI to enter words.
"""

import threading
import uvicorn
import os
from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import json

import firebase_admin
from firebase_admin import credentials, messaging

# ─── Firebase Init ───────────────────────────────────────────────────

SERVICE_ACCOUNT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wordcomet.json")
TOKEN_STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fcm_tokens.json")

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

# ─── App Setup ───────────────────────────────────────────────────────

app = FastAPI(title="Word of the Day API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── In-Memory State ────────────────────────────────────────────────

current_word: dict | None = None

# ─── Pydantic Models ────────────────────────────────────────────────

class TokenRegister(BaseModel):
    token: str

# ─── Public Endpoints ───────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "Word of the Day",
        "status": "running",
        "has_word": current_word is not None,
        "registered_devices": len(fcm_tokens),
    }


@app.get("/word-of-the-day")
def get_word():
    """Full word with all details."""
    if not current_word:
        raise HTTPException(status_code=404, detail="No word set for today yet.")
    return current_word


@app.get("/word-of-the-day/brief")
def get_word_brief():
    """Just word + meaning + part_of_speech."""
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
    """App/extension calls this to register for push notifications."""
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


# ─── Push Notification Helper ───────────────────────────────────────

def send_push_notification(word_data: dict):
    """Send FCM notification to all registered devices."""
    if not firebase_admin._apps:
        print("⚠️  Firebase not initialized — skipping push.")
        return

    if not fcm_tokens:
        print("📭 No registered devices — skipping push.")
        return

    # Clean out stale tokens
    stale = set()
    success_count = 0

    for token in list(fcm_tokens):
        message = messaging.Message(
            notification=messaging.Notification(
                title=f"📖 Word of the Day: {word_data['word']}",
                body=f"{word_data['meaning']}",
            ),
            data={
                "word": word_data["word"],
                "meaning": word_data["meaning"],
                "part_of_speech": word_data["part_of_speech"],
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

    # Remove stale tokens
    if stale:
        fcm_tokens.difference_update(stale)
        _save_tokens(fcm_tokens)
        print(f"   🧹 Removed {len(stale)} stale token(s).")

    print(f"   📬 Sent to {success_count}/{len(fcm_tokens) + len(stale)} device(s).")


# ─── Admin CLI (runs in main thread) ────────────────────────────────

def admin_cli():
    global current_word

    print("\n" + "=" * 50)
    print("  Word of the Day — Server Running!")
    print("  API: http://127.0.0.1:8011")
    print("=" * 50)

    while True:
        print("\n📝 Set Today's Word (type 'quit' to stop)\n" + "-" * 30)

        word = input("Word: ").strip()
        if word.lower() == "quit":
            print("👋 Shutting down.")
            os._exit(0)

        if not word:
            print("❌ Word cannot be empty.")
            continue

        meaning = input("Meaning: ").strip()
        if not meaning:
            print("❌ Meaning cannot be empty.")
            continue

        part_of_speech = input("Part of speech (noun/verb/adjective/adverb): ").strip()
        if not part_of_speech:
            print("❌ Part of speech cannot be empty.")
            continue

        example = input("Example sentence (optional): ").strip() or None
        pronunciation = input("Pronunciation e.g. 'ih-FEM-er-uhl' (optional): ").strip() or None
        phonetics = input("Phonetics IPA e.g. '/ɪˈfɛm.ər.əl/' (optional): ").strip() or None
        language = input("Language/origin (default: English): ").strip() or "English"

        current_word = {
            "word": word,
            "meaning": meaning,
            "part_of_speech": part_of_speech,
            "example": example,
            "pronunciation": pronunciation,
            "phonetics": phonetics,
            "language": language,
            "date": str(date.today()),
        }

        print(f"\n✅ '{word}' is now live!")
        print(f"   GET http://127.0.0.1:8011/word-of-the-day to see it.")

        # Send push notifications
        send_notify = input("Send push notification? (y/n, default y): ").strip().lower()
        if send_notify != "n":
            print("   📤 Sending push notifications...")
            send_push_notification(current_word)


# ─── Run ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start server in background thread
    server_thread = threading.Thread(
        target=uvicorn.run,
        args=(app,),
        kwargs={"host": "0.0.0.0", "port": 8011, "log_level": "warning"},
        daemon=True,
    )
    server_thread.start()

    # Admin CLI in main thread
    admin_cli()