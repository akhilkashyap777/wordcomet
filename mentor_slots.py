"""
WordComet — Mentor Availability, Booking Slots, and Timed Video WebSocket

Add this file beside your other router files.

Main app include example:
    from mentor_slots import router as mentor_slots_router
    app.include_router(mentor_slots_router)

Required tables:
    mentor_availability
    mentor_bookings
"""

import os
import json
import uuid
import asyncio
import psycopg2
import psycopg2.extras
from datetime import date, datetime, time, timedelta
from typing import Optional, List, Dict

from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from firebase_admin import auth as fb_auth
from typing import Optional
from fastapi import HTTPException, Query
import boto3
from fastapi import UploadFile, File, Form

load_dotenv()

router = APIRouter(prefix="/mentor", tags=["Mentor Slots"])
bearer_scheme = HTTPBearer()

# Small grace period so users can join a little early and reconnect briefly.
# Set both to 0 if you want exact start/end only.
JOIN_GRACE_MINUTES = int(os.environ.get("MENTOR_CALL_JOIN_GRACE_MINUTES", "5"))
END_GRACE_MINUTES = int(os.environ.get("MENTOR_CALL_END_GRACE_MINUTES", "0"))

PUBLIC_R2_URL = "https://pub-1d52713b8d67432eb2e7800bd9e02e11.r2.dev"
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

def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )

# ─── Auth Helpers ────────────────────────────────────────────────────

def verify_firebase_token(token: str) -> dict:
    try:
        return fb_auth.verify_id_token(token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {str(e)}")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    return verify_firebase_token(credentials.credentials)


def get_db_user_by_firebase_uid(firebase_uid: str) -> dict:
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, firebase_uid, email, display_name, role, is_active
            FROM users
            WHERE firebase_uid = %s
            """,
            (firebase_uid,),
        )
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="User not found in database.")
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="Account is deactivated.")
        return dict(user)
    finally:
        conn.close()


def require_role(db_user: dict, role: str):
    if db_user["role"] != role:
        raise HTTPException(status_code=403, detail=f"Only {role}s can perform this action.")


# ─── Pydantic Models ─────────────────────────────────────────────────

class CreateAvailabilityBody(BaseModel):
    availability_date: date
    start_time: str = Field(..., examples=["19:00"])
    end_time: str = Field(..., examples=["21:00"])
    slot_duration_minutes: int = Field(60, gt=0, examples=[60])


class UpdateAvailabilityBody(BaseModel):
    day_of_week: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    slot_duration_minutes: Optional[int] = Field(None, gt=0)
    is_active: Optional[bool] = None


class BookSlotBody(BaseModel):
    mentor_id: int
    availability_id: Optional[int] = None
    session_date: date
    start_time: str = Field(..., examples=["19:00"])
    end_time: str = Field(..., examples=["20:00"])


VALID_DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}

DESIGNATIONS = [
    "Software Development",
    "Frontend Development",
    "Backend Development",
    "Full Stack Development",
    "Mobile App Development",
    "Cloud Computing",
    "DevOps",
    "Data Science & AI",
    "Cyber Security",
    "Quality Assurance (QA)",
    "UI/UX Design",
    "Product Management"
]


# ─── Time Helpers ────────────────────────────────────────────────────

def parse_hhmm(value: str) -> time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise HTTPException(status_code=400, detail="Time must be in HH:MM format, example: 19:00")


def combine_utc(session_date: date, slot_time: time) -> datetime:
    # Your DB stores DATE + TIME separately. This treats them as UTC.
    # If your frontend uses Asia/Kolkata local time, convert before storing or add timezone column later.
    return datetime.combine(session_date, slot_time).replace(tzinfo=None)


def generate_slots_for_date(availability: dict, session_date: date) -> List[dict]:
    start_dt = datetime.combine(session_date, availability["start_time"])
    end_dt = datetime.combine(session_date, availability["end_time"])
    duration = timedelta(minutes=availability["slot_duration_minutes"])

    slots = []
    current = start_dt
    while current + duration <= end_dt:
        slots.append({
            "availability_id": availability["id"],
            "mentor_id": availability["mentor_id"],
            "session_date": session_date.isoformat(),
            "start_time": current.time().strftime("%H:%M"),
            "end_time": (current + duration).time().strftime("%H:%M"),
        })
        current += duration
    return slots


# ─── Availability APIs ───────────────────────────────────────────────

@router.post("/availability")
def create_availability(
    body: CreateAvailabilityBody,
    current_user: dict = Depends(get_current_user),
):
    """
    Mentor creates available range.
    Example: Monday 19:00 to 21:00 with 60 min duration creates 2 possible slots.
    """
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentor")

    start_t = parse_hhmm(body.start_time)
    end_t = parse_hhmm(body.end_time)
    if start_t >= end_t:
        raise HTTPException(status_code=400, detail="start_time must be before end_time.")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO mentor_availability
                (mentor_id, availability_date, start_time, end_time, slot_duration_minutes, is_active, created_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, NOW())
            RETURNING *
            """,
            (db_user["id"], body.availability_date, start_t, end_t, body.slot_duration_minutes),
        )
        availability = dict(cur.fetchone())
        conn.commit()
        return {"status": "created", "availability": availability}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Create availability failed: {str(e)}")
    finally:
        conn.close()


@router.get("/availability/me")
def my_availability(current_user: dict = Depends(get_current_user)):
    """Mentor sees their own availability ranges."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentor")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT *
            FROM mentor_availability
            WHERE mentor_id = %s
            ORDER BY id DESC
            """,
            (db_user["id"],),
        )
        return {"availability": [dict(row) for row in cur.fetchall()]}
    finally:
        conn.close()


@router.patch("/availability/{availability_id}")
def update_availability(
    availability_id: int,
    body: UpdateAvailabilityBody,
    current_user: dict = Depends(get_current_user),
):
    """Mentor updates/deactivates their own availability."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentor")

    fields = {}
    if body.day_of_week is not None:
        if body.day_of_week not in VALID_DAYS:
            raise HTTPException(status_code=400, detail="Invalid day_of_week.")
        fields["day_of_week"] = body.day_of_week
    if body.start_time is not None:
        fields["start_time"] = parse_hhmm(body.start_time)
    if body.end_time is not None:
        fields["end_time"] = parse_hhmm(body.end_time)
    if body.slot_duration_minutes is not None:
        fields["slot_duration_minutes"] = body.slot_duration_minutes
    if body.is_active is not None:
        fields["is_active"] = body.is_active

    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    set_clause = ", ".join(f"{key} = %s" for key in fields.keys())
    values = list(fields.values()) + [availability_id, db_user["id"]]

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            UPDATE mentor_availability
            SET {set_clause}
            WHERE id = %s AND mentor_id = %s
            RETURNING *
            """,
            values,
        )
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="Availability not found.")
        conn.commit()
        return {"status": "updated", "availability": dict(updated)}
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Update availability failed: {str(e)}")
    finally:
        conn.close()


@router.get("/{mentor_id}/slots")
def get_mentor_slots(mentor_id: int, session_date: date):
    """
    Shows generated slots for one mentor on one date.
    Already booked slots are returned as is_booked=true.
    """

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT id, role FROM users WHERE id = %s AND is_active = TRUE", (mentor_id,))
        mentor = cur.fetchone()
        if not mentor:
            raise HTTPException(status_code=404, detail="Mentor not found.")
        if mentor["role"] != "mentor":
            raise HTTPException(status_code=400, detail="Selected user is not a mentor.")

        cur.execute(
            """
            SELECT *
            FROM mentor_availability
            WHERE mentor_id = %s
                AND availability_date = %s
                AND is_active = TRUE
            ORDER BY start_time
            """,
            (mentor_id, session_date),
        )
        availability_rows = cur.fetchall()

        all_slots = []
        for availability in availability_rows:
            all_slots.extend(generate_slots_for_date(dict(availability), session_date))

        cur.execute(
            """
            SELECT start_time, end_time
            FROM mentor_bookings
            WHERE mentor_id = %s
              AND session_date = %s
              AND status = 'booked'
            """,
            (mentor_id, session_date),
        )
        booked = {
            (row["start_time"].strftime("%H:%M"), row["end_time"].strftime("%H:%M"))
            for row in cur.fetchall()
        }

        now = datetime.utcnow()
        for slot in all_slots:
            slot_key = (slot["start_time"], slot["end_time"])
            slot_start = datetime.combine(session_date, parse_hhmm(slot["start_time"]))
            slot["is_booked"] = slot_key in booked
            slot["is_past"] = slot_start <= now
            slot["can_book"] = (not slot["is_booked"]) and (not slot["is_past"])

        return {"mentor_id": mentor_id, "session_date": session_date.isoformat(), "slots": all_slots}

    finally:
        conn.close()


# ─── Booking APIs ────────────────────────────────────────────────────

@router.post("/bookings")
def book_slot(
    mentor_id: int = Form(...),
    availability_id: Optional[int] = Form(None),
    session_date: date = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    resume: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):

    body = BookSlotBody(
        mentor_id=mentor_id,
        availability_id=availability_id,
        session_date=session_date,
        start_time=start_time,
        end_time=end_time,
    )
    """Mentee books one slot with a mentor."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentee")

    start_t = parse_hhmm(body.start_time)
    end_t = parse_hhmm(body.end_time)
    if start_t >= end_t:
        raise HTTPException(status_code=400, detail="start_time must be before end_time.")

    if datetime.combine(body.session_date, start_t) <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Cannot book a past slot.")

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("SELECT id, role FROM users WHERE id = %s AND is_active = TRUE", (body.mentor_id,))
        mentor = cur.fetchone()
        if not mentor:
            raise HTTPException(status_code=404, detail="Mentor not found.")
        if mentor["role"] != "mentor":
            raise HTTPException(status_code=400, detail="Selected user is not a mentor.")

        # Confirm this slot is inside mentor availability.
        if body.availability_id:
            cur.execute(
                """
                SELECT * FROM mentor_availability
                WHERE id = %s AND mentor_id = %s AND availability_date = %s AND is_active = TRUE
                """,
                (body.availability_id, body.mentor_id, body.session_date),
            )
        else:
            cur.execute(
                """
                SELECT * FROM mentor_availability
                WHERE mentor_id = %s
                  AND availability_date = %s
                  AND start_time <= %s
                  AND end_time >= %s
                  AND is_active = TRUE
                ORDER BY start_time
                LIMIT 1
                """,
                (body.mentor_id, body.session_date, start_t, end_t),
            )

        availability = cur.fetchone()
        if not availability:
            raise HTTPException(status_code=400, detail="This slot is not inside mentor availability.")

        # Confirm slot matches duration boundaries generated from that availability.
        generated_slots = generate_slots_for_date(dict(availability), body.session_date)
        valid_slot = any(s["start_time"] == body.start_time and s["end_time"] == body.end_time for s in generated_slots)
        if not valid_slot:
            raise HTTPException(status_code=400, detail="Invalid slot timing for this availability.")

        video_room_id = f"mentor-{body.mentor_id}-mentee-{db_user['id']}-{body.session_date.strftime('%Y%m%d')}-{start_t.strftime('%H%M')}-{uuid.uuid4().hex[:8]}"

        cur.execute(
            """
            INSERT INTO mentor_bookings
                (mentor_id, mentee_id, availability_id, session_date, start_time, end_time, status, video_room_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'booked', %s, NOW())
            RETURNING *
            """,
            (
                body.mentor_id,
                db_user["id"],
                availability["id"],
                body.session_date,
                start_t,
                end_t,
                video_room_id,
            ),
        )
        booking = dict(cur.fetchone())
        booking_id = booking["id"]

        allowed_content_types = [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ]

        if resume.content_type not in allowed_content_types:
            raise HTTPException(
                status_code=400,
                detail="Only PDF, DOC, or DOCX resumes are allowed.",
            )

        bucket_name = os.environ.get("R2_BUCKET_NAME")
        folder = os.environ.get("R2_RESUME_FOLDER", "resumes")

        if not bucket_name:
            raise HTTPException(
                status_code=500,
                detail="R2_BUCKET_NAME missing in env.",
            )

        file_ext = resume.filename.split(".")[-1].lower()

        resume_key = (
            f"{folder}/mentee_{db_user['id']}/"
            f"booking_{booking_id}/resume.{file_ext}"
        )

        r2 = get_r2_client()

        r2.upload_fileobj(
            resume.file,
            bucket_name,
            resume_key,
            ExtraArgs={
                "ContentType": resume.content_type,
            },
        )

        cur.execute(
            """
            UPDATE mentor_bookings
            SET
                resume_key = %s,
                resume_filename = %s,
                resume_uploaded_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (
                resume_key,
                resume.filename,
                booking_id,
            ),
        )

        booking = dict(cur.fetchone())

        conn.commit()

        return {
            "status": "booked",
            "message": "Booking created and resume uploaded successfully.",
            "booking": booking,
        }

    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="This mentor slot is already booked.")
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Booking failed: {str(e)}")
    finally:
        conn.close()


@router.get("/bookings/me")
def my_bookings(current_user: dict = Depends(get_current_user)):
    """Mentor or mentee sees their own bookings."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])

    conn = get_db()
    try:
        cur = conn.cursor()
        if db_user["role"] == "mentor":
            cur.execute(
                """
                SELECT b.*, u.display_name AS mentee_name, u.profile_picture_url AS mentee_picture
                FROM mentor_bookings b
                JOIN users u ON u.id = b.mentee_id
                WHERE b.mentor_id = %s
                    AND b.status = 'booked'
                    AND b.session_date = CURRENT_DATE
                    AND CURRENT_TIME BETWEEN b.start_time AND b.end_time
                ORDER BY b.session_date DESC, b.start_time DESC
                """,
                (db_user["id"],),
            )
        else:
            cur.execute(
                """
                SELECT b.*, u.display_name AS mentor_name, u.profile_picture_url AS mentor_picture
                FROM mentor_bookings b
                JOIN users u ON u.id = b.mentor_id
                WHERE b.mentee_id = %s
                    AND b.status = 'booked'
                    AND b.session_date = CURRENT_DATE
                    AND CURRENT_TIME BETWEEN b.start_time AND b.end_time
                ORDER BY b.session_date DESC, b.start_time DESC
                """,
                (db_user["id"],),
            )
        bookings = [dict(row) for row in cur.fetchall()]

        if not bookings:
            raise HTTPException(
                status_code=404,
                detail="No active booked session is available right now."
            )

        return {"bookings": bookings}
    finally:
        conn.close()


@router.patch("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, current_user: dict = Depends(get_current_user)):
    """Mentor or mentee can cancel their own booking."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE mentor_bookings
            SET status = 'cancelled'
            WHERE id = %s
              AND status = 'booked'
              AND (mentor_id = %s OR mentee_id = %s)
            RETURNING *
            """,
            (booking_id, db_user["id"], db_user["id"]),
        )
        booking = cur.fetchone()
        if not booking:
            raise HTTPException(status_code=404, detail="Active booking not found.")
        conn.commit()
        return {"status": "cancelled", "booking": dict(booking)}
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Cancel booking failed: {str(e)}")
    finally:
        conn.close()

@router.post("/bookings/{booking_id}/resume")
def upload_booking_resume(
    booking_id: int,
    resume: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentee")

    if resume.content_type not in [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]:
        raise HTTPException(status_code=400, detail="Only PDF, DOC, or DOCX resumes are allowed.")

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM mentor_bookings
            WHERE id = %s
              AND mentee_id = %s
              AND status = 'booked'
            """,
            (booking_id, db_user["id"]),
        )

        booking = cur.fetchone()

        if not booking:
            raise HTTPException(status_code=404, detail="Active booking not found for this mentee.")

        bucket_name = os.environ.get("R2_BUCKET_NAME")
        folder = os.environ.get("R2_RESUME_FOLDER", "resumes")

        if not bucket_name:
            raise HTTPException(status_code=500, detail="R2_BUCKET_NAME missing in env.")

        file_ext = resume.filename.split(".")[-1].lower()
        resume_key = f"{folder}/mentee_{db_user['id']}/booking_{booking_id}/resume.{file_ext}"

        r2 = get_r2_client()

        r2.upload_fileobj(
            resume.file,
            bucket_name,
            resume_key,
            ExtraArgs={
                "ContentType": resume.content_type,
            },
        )

        cur.execute(
            """
            UPDATE mentor_bookings
            SET
                resume_key = %s,
                resume_filename = %s,
                resume_uploaded_at = NOW()
            WHERE id = %s
            RETURNING *
            """,
            (resume_key, resume.filename, booking_id),
        )

        updated_booking = dict(cur.fetchone())
        conn.commit()

        return {
            "status": "resume_uploaded",
            "booking": updated_booking,
        }

    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Resume upload failed: {str(e)}")
    finally:
        conn.close()


# ─── Timed WebSocket for Video Signaling ─────────────────────────────

active_rooms: Dict[str, List[WebSocket]] = {}
room_lock = asyncio.Lock()


def get_booking_for_socket(video_room_id: str, firebase_uid: str) -> dict:
    """
    Allows WebSocket only when:
      1. booking exists and status is booked
      2. logged-in user is mentor or mentee for this booking
      3. current UTC time is inside session window, with optional grace
    """
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.*,
                u.id AS current_user_id,
                u.role AS current_user_role
            FROM mentor_bookings b
            JOIN users u ON u.firebase_uid = %s
            WHERE b.video_room_id = %s
              AND b.status = 'booked'
              AND u.is_active = TRUE
              AND (u.id = b.mentor_id OR u.id = b.mentee_id)
            """,
            (firebase_uid, video_room_id),
        )
        booking = cur.fetchone()
        if not booking:
            raise HTTPException(status_code=403, detail="No active booking found for this user and room.")

        start_dt = datetime.combine(booking["session_date"], booking["start_time"]) - timedelta(minutes=JOIN_GRACE_MINUTES)
        end_dt = datetime.combine(booking["session_date"], booking["end_time"]) + timedelta(minutes=END_GRACE_MINUTES)
        now = datetime.utcnow()

        if now < start_dt:
            raise HTTPException(status_code=403, detail="Call is not open yet.")
        if now > end_dt:
            raise HTTPException(status_code=403, detail="Call time has ended.")

        return dict(booking)
    finally:
        conn.close()


@router.websocket("/ws/video/{video_room_id}")
async def mentor_video_socket(websocket: WebSocket, video_room_id: str):
    """
    Timed WebSocket room for WebRTC signaling.

    Frontend connects like:
      wss://your-domain.com/mentor/ws/video/<video_room_id>?token=<firebase_id_token>

    This WebSocket only opens during the booked slot time.
    Only the booking's mentor and mentee can join.
    Maximum 2 connections per room.
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        decoded = fb_auth.verify_id_token(token)
        booking = get_booking_for_socket(video_room_id, decoded["uid"])
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    async with room_lock:
        room = active_rooms.setdefault(video_room_id, [])
        if len(room) >= 2:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        room.append(websocket)

    await websocket.send_text(json.dumps({
        "type": "joined",
        "room": video_room_id,
        "booking_id": booking["id"],
        "role": booking["current_user_role"],
    }))

    try:
        while True:
            # Re-check time on every message, so the room stops after the slot ends.
            try:
                get_booking_for_socket(video_room_id, decoded["uid"])
            except Exception:
                await websocket.send_text(json.dumps({"type": "call_ended", "reason": "slot_time_ended"}))
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                break

            message = await websocket.receive_text()

            # Relay SDP/ICE/chat-control messages to the other participant only.
            async with room_lock:
                peers = list(active_rooms.get(video_room_id, []))
            for peer in peers:
                if peer is not websocket:
                    await peer.send_text(message)

    except WebSocketDisconnect:
        pass
    finally:
        async with room_lock:
            room = active_rooms.get(video_room_id, [])
            if websocket in room:
                room.remove(websocket)
            if not room and video_room_id in active_rooms:
                del active_rooms[video_room_id]

@router.get("/bookings/{booking_id}/student-details")
def mentor_get_student_booking_details(
    booking_id: int,
    current_user: dict = Depends(get_current_user),
):
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentor")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id AS booking_id,
                b.session_date,
                b.start_time,
                b.end_time,
                b.status,
                b.video_room_id,
                b.resume_key,
                b.resume_filename,
                b.resume_uploaded_at,

                u.id AS student_id,
                u.display_name AS student_name,
                u.full_name AS student_full_name,
                u.email AS student_email,
                u.profile_picture_url AS student_picture,

                u.qualification

            FROM mentor_bookings b
            JOIN users u ON u.id = b.mentee_id
            WHERE b.id = %s
              AND b.mentor_id = %s
              AND b.status = 'booked'
            """,
            (booking_id, db_user["id"]),
        )

        row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Booking not found for this mentor."
            )

        return {
            "booking_id": row["booking_id"],
            "session_date": row["session_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "status": row["status"],
            "video_room_id": row["video_room_id"],

            "student": {
                "id": row["student_id"],
                "display_name": row["student_name"],
                "full_name": row["student_full_name"],
                "email": row["student_email"],
                "profile_picture_url": row["student_picture"],
                "qualification": row["qualification"],
            },

            "resume": {
                "resume_key": row["resume_key"],
                "resume_url": (
                    f"{PUBLIC_R2_URL}/{row['resume_key']}"
                    if row["resume_key"]
                    else None
                ),
                "filename": row["resume_filename"],
                "uploaded_at": row["resume_uploaded_at"],
                "is_uploaded": row["resume_key"] is not None,
            }
        }

    finally:
        conn.close()

@router.get("/bookings/{booking_id}/mentor-details")
def mentee_get_mentor_booking_details(
    booking_id: int,
    current_user: dict = Depends(get_current_user),
):
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentee")

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                b.id AS booking_id,
                b.session_date,
                b.start_time,
                b.end_time,
                b.status,
                b.video_room_id,
                b.resume_key,
                b.resume_filename,
                b.resume_uploaded_at,

                u.id AS mentor_id,
                u.display_name AS mentor_name,
                u.full_name AS mentor_full_name,
                u.email AS mentor_email,
                u.profile_picture_url AS mentor_picture,
                u.bio AS mentor_bio,
                u.average_rating,
                u.rating_count

            FROM mentor_bookings b
            JOIN users u ON u.id = b.mentor_id
            WHERE b.id = %s
              AND b.mentee_id = %s
              AND b.status = 'booked'
            """,
            (booking_id, db_user["id"]),
        )

        row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail="Booking not found for this student."
            )

        return {
            "booking_id": row["booking_id"],
            "session_date": row["session_date"],
            "start_time": row["start_time"],
            "end_time": row["end_time"],
            "status": row["status"],
            "video_room_id": row["video_room_id"],

            "mentor": {
                "id": row["mentor_id"],
                "display_name": row["mentor_name"],
                "full_name": row["mentor_full_name"],
                "email": row["mentor_email"],
                "profile_picture_url": row["mentor_picture"],
                "bio": row["mentor_bio"],
                "average_rating": row["average_rating"],
                "rating_count": row["rating_count"],
            },

            "resume": {
                "resume_key": row["resume_key"],
                "resume_url": (
                    f"{PUBLIC_R2_URL}/{row['resume_key']}"
                    if row["resume_key"]
                    else None
                ),
                "filename": row["resume_filename"],
                "uploaded_at": row["resume_uploaded_at"],
                "is_uploaded": row["resume_key"] is not None,
            }
        }

    finally:
        conn.close()

@router.get("/bookings/all")
def get_all_my_bookings(
    current_user: dict = Depends(get_current_user),
):
    """
    Return all bookings belonging to the logged-in mentor or mentee.

    Categories:
    - active
    - upcoming
    - past
    - cancelled
    """

    db_user = get_db_user_by_firebase_uid(current_user["uid"])

    conn = get_db()

    try:
        cur = conn.cursor()

        if db_user["role"] == "mentor":
            cur.execute(
                """
                SELECT
                    b.*,

                    u.id AS participant_id,
                    u.display_name AS participant_name,
                    u.full_name AS participant_full_name,
                    u.email AS participant_email,
                    u.profile_picture_url AS participant_picture,
                    u.qualification AS participant_qualification

                FROM mentor_bookings b

                JOIN users u
                    ON u.id = b.mentee_id

                WHERE b.mentor_id = %s

                ORDER BY
                    b.session_date DESC,
                    b.start_time DESC
                """,
                (db_user["id"],),
            )

        else:
            cur.execute(
                """
                SELECT
                    b.*,

                    u.id AS participant_id,
                    u.display_name AS participant_name,
                    u.full_name AS participant_full_name,
                    u.email AS participant_email,
                    u.profile_picture_url AS participant_picture,
                    u.bio AS participant_bio,
                    u.average_rating AS participant_average_rating,
                    u.rating_count AS participant_rating_count

                FROM mentor_bookings b

                JOIN users u
                    ON u.id = b.mentor_id

                WHERE b.mentee_id = %s

                ORDER BY
                    b.session_date DESC,
                    b.start_time DESC
                """,
                (db_user["id"],),
            )

        rows = cur.fetchall()

        now = datetime.utcnow()

        active_bookings = []
        upcoming_bookings = []
        past_bookings = []
        cancelled_bookings = []

        for row in rows:
            booking = dict(row)

            booking["resume_url"] = (
                f"{PUBLIC_R2_URL}/{booking['resume_key']}"
                if booking.get("resume_key")
                else None
            )

            session_start = datetime.combine(
                booking["session_date"],
                booking["start_time"],
            )

            session_end = datetime.combine(
                booking["session_date"],
                booking["end_time"],
            )

            if booking["status"] == "cancelled":
                booking["dashboard_status"] = "cancelled"
                cancelled_bookings.append(booking)

            elif booking["status"] == "booked" and session_start <= now <= session_end:
                booking["dashboard_status"] = "active"
                active_bookings.append(booking)

            elif booking["status"] == "booked" and session_start > now:
                booking["dashboard_status"] = "upcoming"
                upcoming_bookings.append(booking)

            elif booking["status"] == "booked" and session_end < now:
                booking["dashboard_status"] = "past"
                past_bookings.append(booking)

            else:
                booking["dashboard_status"] = booking["status"]
                past_bookings.append(booking)

        # Upcoming bookings should appear nearest-first.
        upcoming_bookings.sort(
            key=lambda booking: (
                booking["session_date"],
                booking["start_time"],
            )
        )

        # Most recent past and cancelled bookings first.
        past_bookings.sort(
            key=lambda booking: (
                booking["session_date"],
                booking["start_time"],
            ),
            reverse=True,
        )

        cancelled_bookings.sort(
            key=lambda booking: (
                booking["session_date"],
                booking["start_time"],
            ),
            reverse=True,
        )

        return {
            "role": db_user["role"],

            "summary": {
                "total": len(rows),
                "active": len(active_bookings),
                "upcoming": len(upcoming_bookings),
                "past": len(past_bookings),
                "cancelled": len(cancelled_bookings),
            },

            "next_booking": (
                upcoming_bookings[0]
                if upcoming_bookings
                else None
            ),

            "bookings": {
                "active": active_bookings,
                "upcoming": upcoming_bookings,
                "past": past_bookings,
                "cancelled": cancelled_bookings,
            },
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load bookings: {str(e)}",
        )

    finally:
        conn.close()

@router.get("/bydesignation")
def get_mentors_by_designation(
    designation: str = Query(...),
):
    if designation not in DESIGNATIONS:
        raise HTTPException(
            status_code=404,
            detail="Designation not found.",
        )

    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                id,
                firebase_uid,
                display_name,
                full_name,
                profile_picture_url,
                bio,
                designation,
                experience_years,
                experience_months,
                average_rating,
                rating_count
            FROM users
            WHERE role = 'mentor'
              AND is_active = TRUE
              AND designation = %s
            ORDER BY average_rating DESC, rating_count DESC, id ASC
            """,
            (designation,),
        )

        mentors = [dict(row) for row in cur.fetchall()]

        return {
            "status": "ok",
            "designation": designation,
            "mentor_count": len(mentors),
            "mentors": mentors,
        }

    finally:
        conn.close()

@router.get("/bydate")
def get_mentors_by_date(
    session_date: date = Query(...),
):
    """
    Return mentors who have at least one available slot
    on the selected date.

    Example:
    GET /mentor/bydate?session_date=2026-08-05
    """

    if session_date < date.today():
        raise HTTPException(
            status_code=400,
            detail="Cannot search mentors for a past date.",
        )

    conn = get_db()

    try:
        cur = conn.cursor()

        # Get all active availability ranges for the selected date,
        # together with mentor profile details.
        cur.execute(
            """
            SELECT
                ma.id AS availability_id,
                ma.mentor_id,
                ma.availability_date,
                ma.start_time AS availability_start_time,
                ma.end_time AS availability_end_time,
                ma.slot_duration_minutes,

                u.firebase_uid,
                u.display_name,
                u.full_name,
                u.profile_picture_url,
                u.bio,
                u.designation,
                u.experience_years,
                u.experience_months,
                u.average_rating,
                u.rating_count

            FROM mentor_availability ma

            JOIN users u
                ON u.id = ma.mentor_id

            WHERE ma.availability_date = %s
              AND ma.is_active = TRUE
              AND u.role = 'mentor'
              AND u.is_active = TRUE

            ORDER BY
                u.average_rating DESC,
                u.rating_count DESC,
                ma.start_time ASC
            """,
            (session_date,),
        )

        availability_rows = cur.fetchall()

        if not availability_rows:
            return {
                "status": "ok",
                "session_date": session_date.isoformat(),
                "mentor_count": 0,
                "mentors": [],
            }

        mentor_ids = list({
            row["mentor_id"]
            for row in availability_rows
        })

        # Get all booked slots for these mentors on the selected date.
        cur.execute(
            """
            SELECT
                mentor_id,
                start_time,
                end_time

            FROM mentor_bookings

            WHERE mentor_id = ANY(%s)
              AND session_date = %s
              AND status = 'booked'
            """,
            (mentor_ids, session_date),
        )

        booked_rows = cur.fetchall()

        booked_slots = {}

        for row in booked_rows:
            mentor_id = row["mentor_id"]

            if mentor_id not in booked_slots:
                booked_slots[mentor_id] = set()

            booked_slots[mentor_id].add(
                (
                    row["start_time"].strftime("%H:%M"),
                    row["end_time"].strftime("%H:%M"),
                )
            )

        mentors_map = {}
        now = datetime.utcnow()

        for row in availability_rows:
            mentor_id = row["mentor_id"]

            if mentor_id not in mentors_map:

                mentors_map[mentor_id] = {
                    "id": mentor_id,
                    "firebase_uid": row["firebase_uid"],
                    "display_name": row["display_name"],
                    "full_name": row["full_name"],
                    "profile_picture_url": row["profile_picture_url"],
                    "bio": row["bio"],
                    "designation": row["designation"],
                    "experience_years": row["experience_years"],
                    "experience_months": row["experience_months"],
                    "average_rating": row["average_rating"],
                    "rating_count": row["rating_count"],
                    "availability": [],
                    "available_slots": [],
                }

            availability = {
                "id": row["availability_id"],
                "mentor_id": row["mentor_id"],
                "start_time": row["availability_start_time"],
                "end_time": row["availability_end_time"],
                "slot_duration_minutes": row["slot_duration_minutes"],
            }

            mentors_map[mentor_id]["availability"].append({
                "availability_id": row["availability_id"],
                "start_time": row["availability_start_time"].strftime("%H:%M"),
                "end_time": row["availability_end_time"].strftime("%H:%M"),
                "slot_duration_minutes": row["slot_duration_minutes"],
            })

            generated_slots = generate_slots_for_date(
                availability,
                session_date,
            )

            mentor_booked_slots = booked_slots.get(
                mentor_id,
                set(),
            )

            for slot in generated_slots:
                slot_key = (
                    slot["start_time"],
                    slot["end_time"],
                )

                slot_start = datetime.combine(
                    session_date,
                    parse_hhmm(slot["start_time"]),
                )

                is_booked = slot_key in mentor_booked_slots
                is_past = slot_start <= now

                if is_booked or is_past:
                    continue

                mentors_map[mentor_id]["available_slots"].append({
                    "availability_id": slot["availability_id"],
                    "session_date": slot["session_date"],
                    "start_time": slot["start_time"],
                    "end_time": slot["end_time"],
                    "duration": row["slot_duration_minutes"],
                    "can_book": True,
                })

        # Only return mentors who still have at least one bookable slot.
        mentors = []

        for mentor in mentors_map.values():
            mentor["available_slot_count"] = len(
                mentor["available_slots"]
            )

            if mentor["available_slot_count"] > 0:
                mentors.append(mentor)

        mentors.sort(
            key=lambda mentor: (
                -(float(mentor["average_rating"] or 0)),
                -(mentor["rating_count"] or 0),
                mentor["id"],
            )
        )

        return {
            "status": "ok",
            "session_date": session_date.isoformat(),
            "mentor_count": len(mentors),
            "mentors": mentors,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load mentors by date: {str(e)}",
        )

    finally:
        conn.close()

# @router.get("/domains")
# def get_domains():
#     return {
#         "status": "ok",
#         "domain_count": len(DOMAIN_DESIGNATIONS),
#         "domains": [
#             {
#                 "designations": designations,
#             }
#             for domain, designations in DOMAIN_DESIGNATIONS.items()
#         ],
#     }

@router.get("/designations")
def get_designations():
    return {
        "status": "ok",
        "designations": DESIGNATIONS,
    }