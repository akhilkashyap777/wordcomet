import os
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel

import firebase_admin
from firebase_admin import messaging

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ADMIN_API_KEY = os.environ.get("WORDCOMET_ADMIN_KEY")

router = APIRouter()

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

def verify_admin(x_api_key: str):
    if x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key.")

# ─── Quiz Endpoints ─────────────────────────────────────────────────

@router.get("/quiz")
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

@router.post("/admin/set-quiz")
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
    from deploy import fcm_tokens, _save_tokens
    
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