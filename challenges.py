"""
WordComet Friend Challenges Router

Add this file as: challenges.py
Then in your main/deploy file add:

    from challenges import router as challenges_router
    app.include_router(challenges_router)

Endpoints:
    POST /challenges/send
    GET  /challenges/pending
    POST /challenges/{challenge_id}/submit
"""

import os
import json
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

# Reuse your existing auth + database objects from friends.py
from friends import db_session, get_current_user


router = APIRouter(prefix="/challenges", tags=["Challenges"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUIZ_STORE_FILE = os.path.join(BASE_DIR, "current_quiz.json")


# -----------------------------
# Pydantic models
# -----------------------------

class SendChallengeInput(BaseModel):
    to_user_id: int
    question_ids: List[int] | None = None


class AnswerInput(BaseModel):
    question_id: int
    selected_index: int


class SubmitChallengeInput(BaseModel):
    answers: List[AnswerInput]


# -----------------------------
# Small DB helpers
# -----------------------------

async def fetch_one(query: str, *args):
    async with db_session.pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch_all(query: str, *args):
    async with db_session.pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def execute(query: str, *args):
    async with db_session.pool.acquire() as conn:
        return await conn.execute(query, *args)


# -----------------------------
# Quiz helpers
# -----------------------------

def load_current_quiz() -> dict:
    if not os.path.exists(QUIZ_STORE_FILE):
        raise HTTPException(status_code=404, detail="No quiz set for today yet.")

    with open(QUIZ_STORE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_questions_by_ids(question_ids: list[int]) -> list[dict]:
    quiz = load_current_quiz()
    questions = quiz.get("questions", [])

    question_map = {q["id"]: q for q in questions}
    selected_questions = []

    for qid in question_ids:
        if qid not in question_map:
            raise HTTPException(status_code=400, detail=f"Invalid question_id: {qid}")
        selected_questions.append(question_map[qid])

    return selected_questions


def calculate_score(question_ids: list[int], answers: list[AnswerInput]) -> int:
    questions = get_questions_by_ids(question_ids)
    correct_map = {q["id"]: q["correct_index"] for q in questions}

    score = 0
    answered_ids = set()

    for ans in answers:
        if ans.question_id not in correct_map:
            raise HTTPException(
                status_code=400,
                detail=f"Question {ans.question_id} does not belong to this challenge."
            )

        if ans.selected_index not in (0, 1, 2, 3):
            raise HTTPException(status_code=400, detail="selected_index must be 0, 1, 2, or 3.")

        if ans.question_id in answered_ids:
            raise HTTPException(status_code=400, detail=f"Duplicate answer for question {ans.question_id}.")

        answered_ids.add(ans.question_id)

        if ans.selected_index == correct_map[ans.question_id]:
            score += 1

    return score


# -----------------------------
# Endpoint 1: Send Challenge
# -----------------------------

@router.post("/send", status_code=201)
async def send_challenge(
    body: SendChallengeInput,
    current_user: dict = Depends(get_current_user),
):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        """
        SELECT id
        FROM users
        WHERE firebase_uid = $1
        """,
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    challenger_id = db_user["id"]
    challenged_id = body.to_user_id

    if challenger_id == challenged_id:
        raise HTTPException(status_code=400, detail="You cannot challenge yourself.")

    # Make sure they are accepted friends
    friendship = await fetch_one(
        """
        SELECT id
        FROM friendships
        WHERE status = 'accepted'
          AND (
              (from_user_id = $1 AND to_user_id = $2)
              OR
              (from_user_id = $2 AND to_user_id = $1)
          )
        """,
        challenger_id,
        challenged_id,
    )

    if not friendship:
        raise HTTPException(status_code=403, detail="You can only challenge accepted friends.")

    quiz = load_current_quiz()
    all_questions = quiz.get("questions", [])

    if not all_questions:
        raise HTTPException(status_code=404, detail="No quiz questions available.")

    if body.question_ids:
        question_ids = body.question_ids
        get_questions_by_ids(question_ids)  # validate ids
    else:
        # Default: use first 5 questions from current quiz
        question_ids = [q["id"] for q in all_questions[:5]]

    challenge = await fetch_one(
        """
        INSERT INTO challenges (
            challenger_id,
            challenged_id,
            word_ids,
            status,
            expires_at
        )
        VALUES ($1, $2, $3::jsonb, 'pending', NOW() + INTERVAL '24 hours')
        RETURNING id, challenger_id, challenged_id, word_ids, status, created_at, expires_at
        """,
        challenger_id,
        challenged_id,
        json.dumps(question_ids),
    )

    return dict(challenge)


# -----------------------------
# Endpoint 2: Pending Challenges
# -----------------------------

@router.get("/pending")
async def get_pending_challenges(current_user: dict = Depends(get_current_user)):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    user_id = db_user["id"]

    rows = await fetch_all(
        """
        SELECT
            c.id,
            c.challenger_id,
            u.display_name AS challenger_name,
            u.profile_picture_url AS challenger_profile_picture_url,
            c.word_ids,
            c.status,
            c.created_at,
            c.expires_at
        FROM challenges c
        JOIN users u ON u.id = c.challenger_id
        WHERE c.challenged_id = $1
          AND c.status IN ('pending', 'in_progress')
          AND c.expires_at > NOW()
        ORDER BY c.created_at DESC
        """,
        user_id,
    )

    return [dict(row) for row in rows]


# -----------------------------
# Endpoint 3: Submit Challenge
# -----------------------------

@router.post("/{challenge_id}/submit")
async def submit_challenge(
    challenge_id: int,
    body: SubmitChallengeInput,
    current_user: dict = Depends(get_current_user),
):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    user_id = db_user["id"]

    challenge = await fetch_one(
        """
        SELECT *
        FROM challenges
        WHERE id = $1
          AND (challenger_id = $2 OR challenged_id = $2)
        """,
        challenge_id,
        user_id,
    )

    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found.")

    challenge = dict(challenge)

    if challenge["status"] == "completed":
        raise HTTPException(status_code=400, detail="Challenge is already completed.")

    if challenge["status"] == "expired" or challenge["expires_at"] < datetime.now(timezone.utc):
        await execute(
            "UPDATE challenges SET status = 'expired' WHERE id = $1",
            challenge_id,
        )
        raise HTTPException(status_code=400, detail="Challenge has expired.")

    question_ids = challenge["word_ids"]
    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    score = calculate_score(question_ids, body.answers)

    # Challenger submitting
    if user_id == challenge["challenger_id"]:
        if challenge["challenger_score"] is not None:
            raise HTTPException(status_code=400, detail="You already submitted this challenge.")

        updated = await fetch_one(
            """
            UPDATE challenges
            SET challenger_score = $1,
                challenger_completed_at = NOW(),
                status = 'in_progress'
            WHERE id = $2
            RETURNING *
            """,
            score,
            challenge_id,
        )

    # Challenged friend submitting
    else:
        if challenge["challenged_score"] is not None:
            raise HTTPException(status_code=400, detail="You already submitted this challenge.")

        updated = await fetch_one(
            """
            UPDATE challenges
            SET challenged_score = $1,
                challenged_completed_at = NOW(),
                status = 'in_progress'
            WHERE id = $2
            RETURNING *
            """,
            score,
            challenge_id,
        )

    updated = dict(updated)

    # If both users submitted, decide winner
    if updated["challenger_score"] is not None and updated["challenged_score"] is not None:
        challenger_score = updated["challenger_score"]
        challenged_score = updated["challenged_score"]

        if challenger_score == challenged_score:
            winner_id = None
            is_tie = True
        elif challenger_score > challenged_score:
            winner_id = updated["challenger_id"]
            is_tie = False
        else:
            winner_id = updated["challenged_id"]
            is_tie = False

        updated = await fetch_one(
            """
            UPDATE challenges
            SET status = 'completed',
                winner_id = $1,
                is_tie = $2
            WHERE id = $3
            RETURNING *
            """,
            winner_id,
            is_tie,
            challenge_id,
        )
        updated = dict(updated)

    return {
        "status": "submitted",
        "your_score": score,
        "challenge": updated,
    }

@router.get("/sent")
async def get_sent_challenges(current_user: dict = Depends(get_current_user)):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    user_id = db_user["id"]

    rows = await fetch_all(
        """
        SELECT
            c.id,
            c.challenger_id,
            c.challenged_id,
            u.display_name AS challenged_name,
            u.profile_picture_url AS challenged_profile_picture_url,
            c.word_ids,
            c.status,
            c.challenger_score,
            c.challenged_score,
            c.winner_id,
            c.is_tie,
            c.created_at,
            c.expires_at
        FROM challenges c
        JOIN users u ON u.id = c.challenged_id
        WHERE c.challenger_id = $1
        ORDER BY c.created_at DESC
        """,
        user_id,
    )

    return [dict(row) for row in rows]

@router.get("/history")
async def get_challenge_history(current_user: dict = Depends(get_current_user)):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    user_id = db_user["id"]

    rows = await fetch_all(
        """
        SELECT
            c.id,
            c.challenger_id,
            challenger.display_name AS challenger_name,
            c.challenged_id,
            challenged.display_name AS challenged_name,
            c.word_ids,
            c.status,
            c.challenger_score,
            c.challenged_score,
            c.winner_id,
            winner.display_name AS winner_name,
            c.is_tie,
            c.created_at,
            c.expires_at
        FROM challenges c
        JOIN users challenger ON challenger.id = c.challenger_id
        JOIN users challenged ON challenged.id = c.challenged_id
        LEFT JOIN users winner ON winner.id = c.winner_id
        WHERE c.challenger_id = $1
           OR c.challenged_id = $1
        ORDER BY c.created_at DESC
        """,
        user_id,
    )

    return [dict(row) for row in rows]

@router.get("/{challenge_id}")
async def get_challenge_details(
    challenge_id: int,
    current_user: dict = Depends(get_current_user),
):
    firebase_uid = current_user["user_id"]

    db_user = await fetch_one(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid,
    )

    if not db_user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in DB.")

    user_id = db_user["id"]

    challenge = await fetch_one(
        """
        SELECT *
        FROM challenges
        WHERE id = $1
          AND (challenger_id = $2 OR challenged_id = $2)
        """,
        challenge_id,
        user_id,
    )

    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found.")

    challenge = dict(challenge)

    question_ids = challenge["word_ids"]
    if isinstance(question_ids, str):
        question_ids = json.loads(question_ids)

    questions = get_questions_by_ids(question_ids)

    safe_questions = []
    for q in questions:
        safe_questions.append({
            "id": q["id"],
            "quiz_type": q["quiz_type"],
            "question": q["question"],
            "options": q["options"],
            "hints": q.get("hints", []),
            "example": q.get("example"),
        })

    return {
        "challenge": challenge,
        "questions": safe_questions,
    }