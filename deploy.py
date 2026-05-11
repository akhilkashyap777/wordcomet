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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import httpx
import uuid

load_dotenv()

import firebase_admin
from firebase_admin import credentials, messaging


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_JSON = os.path.join(BASE_DIR, "wordcomet.json")
TOKEN_STORE_FILE = os.path.join(BASE_DIR, "fcm_tokens.json")
WORD_STORE_FILE = os.path.join(BASE_DIR, "current_word.json")

ADMIN_API_KEY = os.environ.get("WORDCOMET_ADMIN_KEY")

TURN_TOKEN_ID = os.environ.get("TURN_TOKEN_ID")
TURN_API_TOKEN = os.environ.get("TURN_API_TOKEN")

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
        "date": datetime.now().isoformat(timespec="seconds"),
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


class QuizQuestion(BaseModel):
    quiz_type: str
    question: str
    options: list[str]
    correct_index: int
    hints: list[str]
    example: str

class QuizInput(BaseModel):
    questions: list[QuizQuestion]
    send_notification: Optional[bool] = True
    featured_index: Optional[int] = 0

# ─── Quiz Persistence ────────────────────────────────────────────────

QUIZ_STORE_FILE = os.path.join(BASE_DIR, "current_quiz.json")

def _load_quiz() -> dict | None:
    if os.path.exists(QUIZ_STORE_FILE):
        with open(QUIZ_STORE_FILE, "r") as f:
            return json.load(f)
    return None

def _save_quiz(quiz_data: dict):
    with open(QUIZ_STORE_FILE, "w") as f:
        json.dump(quiz_data, f, indent=2)

current_quiz: dict | None = _load_quiz()

# ─── Quiz Endpoints ─────────────────────────────────────────────────

@app.get("/quiz")
def get_quiz():
    """Fetch today's quiz."""
    if not current_quiz:
        raise HTTPException(status_code=404, detail="No quiz set for today yet.")
    return current_quiz


VALID_QUIZ_TYPES = (
    "word_to_meaning", "fill_in_blank", "synonym",
    "antonym", "usage_check", "definition_to_word",
    "part_of_speech", "countability", "refers_to",
    "word_form", "article",
    "tense_identification", "correct_tense_in_sentence"
)

@app.post("/admin/set-quiz")
def set_quiz(
    body: QuizInput,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(...)
):
    global current_quiz
    verify_admin(x_api_key)

    if not body.questions:
        raise HTTPException(status_code=400, detail="At least one question required.")
    if len(body.questions) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 questions per quiz.")

    for idx, q in enumerate(body.questions):
        prefix = f"Question {idx + 1}:"
        if len(q.options) != 4:
            raise HTTPException(status_code=400, detail=f"{prefix} Exactly 4 options required.")
        if q.correct_index not in (0, 1, 2, 3):
            raise HTTPException(status_code=400, detail=f"{prefix} correct_index must be 0-3.")
        if len(q.hints) < 1 or len(q.hints) > 5:
            raise HTTPException(status_code=400, detail=f"{prefix} Provide 1 to 5 hints.")
        if q.quiz_type not in VALID_QUIZ_TYPES:
            raise HTTPException(status_code=400, detail=f"{prefix} Invalid quiz_type.")

    current_quiz = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "total_questions": len(body.questions),
	"featured_index": body.featured_index,
        "questions": [
            {
                "id": idx,
                "quiz_type": q.quiz_type,
                "question": q.question,
                "options": q.options,
                "correct_index": q.correct_index,
                "hints": q.hints,
                "example": q.example,
            }
            for idx, q in enumerate(body.questions)
        ],
    }

    _save_quiz(current_quiz)

    if body.send_notification:
        background_tasks.add_task(send_quiz_notification, current_quiz, body.featured_index)

    return {
        "status": "success",
        "message": f"Quiz with {len(body.questions)} questions is now live!",
        "quiz": current_quiz,
    }


# ─── Quiz Notification ──────────────────────────────────────────────

def send_quiz_notification(quiz_data: dict, featured_index: int = 0):
    """Notify devices with a specific question in the body."""
    if not firebase_admin._apps or not fcm_tokens:
        return

    questions = quiz_data.get("questions", [])
    if not questions:
        return

    if featured_index < 0 or featured_index >= len(questions):
        featured_index = 0

    featured = questions[featured_index]
    total = quiz_data.get("total_questions", len(questions))

    title = "☄️ Can you answer this?"
    body = featured["question"]

    stale = set()
    success_count = 0

    for token in list(fcm_tokens):
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "type": "quiz",
                "total_questions": str(total),
                "featured_index": str(featured_index),
            },
            token=token,
        )
        try:
            messaging.send(message)
            success_count += 1
        except messaging.UnregisteredError:
            stale.add(token)
        except Exception as e:
            print(f"   ⚠️  Quiz notif failed: {e}")

    if stale:
        fcm_tokens.difference_update(stale)
        _save_tokens(fcm_tokens)

    print(f"   📬 Quiz notif sent to {success_count} device(s).")


# ─── WebRTC Signaling ────────────────────────────────────────────────

# Queues for waiting teachers and students
waiting_teachers: list[WebSocket] = []
waiting_students: list[WebSocket] = []

# Active rooms: room_id → {teacher: WebSocket, student: WebSocket}
active_rooms: dict[str, dict] = {}

# Track which room each websocket belongs to
socket_to_room: dict[WebSocket, str] = {}
# Track role of each socket
socket_to_role: dict[WebSocket, str] = {}


async def try_pair():
    """If there's at least one teacher and one student waiting, pair them."""
    if waiting_teachers and waiting_students:
        teacher = waiting_teachers.pop(0)
        student = waiting_students.pop(0)

        room_id = str(uuid.uuid4())
        active_rooms[room_id] = {"teacher": teacher, "student": student}
        socket_to_room[teacher] = room_id
        socket_to_room[student] = room_id

        await teacher.send_text(f'{{"type":"paired","role":"teacher","room_id":"{room_id}"}}')
        await student.send_text(f'{{"type":"paired","role":"student","room_id":"{room_id}"}}')
        print(f"✅ Paired teacher + student in room {room_id}")


async def put_back_in_queue(websocket: WebSocket, role: str):
    """Put a peer back in queue after their partner disconnected."""
    try:
        await websocket.send_text(f'{{"type":"waiting","role":"{role}","reason":"partner_left"}}')
        if role == "teacher":
            waiting_teachers.append(websocket)
            print(f"👨‍🏫 Teacher back in queue — {len(waiting_teachers)} waiting")
        else:
            waiting_students.append(websocket)
            print(f"👨‍🎓 Student back in queue — {len(waiting_students)} waiting")
    except:
        # peer also disconnected, just ignore
        pass


@app.get("/turn-credentials")
async def get_turn_credentials():
    """Generate short-lived TURN credentials for a client."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://rtc.live.cloudflare.com/v1/turn/keys/{TURN_TOKEN_ID}/credentials/generate-ice-servers",
            headers={
                "Authorization": f"Bearer {TURN_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"ttl": 86400}
        )
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to get TURN credentials.")
    return response.json()


@app.websocket("/ws/signal")
async def signaling_endpoint(websocket: WebSocket, role: str = "student"):
    """
    Round-robin matchmaking signaling.
    Connect as: ws://localhost:8011/ws/signal?role=teacher
            or: ws://localhost:8011/ws/signal?role=student
    
    Flow:
    1. Peer connects with role
    2. Server puts them in queue and sends {"type":"waiting"}
    3. When a teacher+student are both in queue, server pairs them
    4. Both get {"type":"paired","role":"...","room_id":"..."}
    5. Peers exchange SDP/ICE through server
    6. If one disconnects, the other goes back to queue automatically
    """
    if role not in ("teacher", "student"):
        await websocket.close(code=1008, reason="role must be teacher or student")
        return

    await websocket.accept()
    socket_to_role[websocket] = role

    # add to queue
    if role == "teacher":
        waiting_teachers.append(websocket)
        print(f"👨‍🏫 Teacher joined queue — {len(waiting_teachers)} waiting")
    else:
        waiting_students.append(websocket)
        print(f"👨‍🎓 Student joined queue — {len(waiting_students)} waiting")

    # notify client they are waiting
    await websocket.send_text(f'{{"type":"waiting","role":"{role}"}}')

    # try to pair immediately
    await try_pair()

    try:
        while True:
            data = await websocket.receive_text()
            room_id = socket_to_room.get(websocket)

            if not room_id:
                # not paired yet, ignore messages
                continue

            if room_id not in active_rooms:
                continue

            # relay message to the other peer
            room = active_rooms[room_id]
            peer = room["student"] if websocket == room["teacher"] else room["teacher"]

            try:
                await peer.send_text(data)
            except:
                # peer connection broken
                pass

    except WebSocketDisconnect:
        print(f"🔌 {role} disconnected")
        room_id = socket_to_room.pop(websocket, None)
        socket_to_role.pop(websocket, None)

        if room_id and room_id in active_rooms:
            # was in an active room
            room = active_rooms.pop(room_id)
            peer = room["student"] if websocket == room["teacher"] else room["teacher"]
            peer_role = "student" if websocket == room["teacher"] else "teacher"

            # clean up peer's room mapping
            socket_to_room.pop(peer, None)

            print(f"👋 Room {room_id} closed — putting {peer_role} back in queue")

            # put the remaining peer back in queue
            await put_back_in_queue(peer, peer_role)

            # try to pair the re-queued peer with someone new
            await try_pair()

        else:
            # was still in queue, just remove
            if websocket in waiting_teachers:
                waiting_teachers.remove(websocket)
                print(f"👨‍🏫 Teacher left queue — {len(waiting_teachers)} remaining")
            if websocket in waiting_students:
                waiting_students.remove(websocket)
                print(f"👨‍🎓 Student left queue — {len(waiting_students)} remaining")


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
