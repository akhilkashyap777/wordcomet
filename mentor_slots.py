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

import boto3
from fastapi import UploadFile, File

load_dotenv()

router = APIRouter(prefix="/mentor", tags=["Mentor Slots"])
bearer_scheme = HTTPBearer()

# Small grace period so users can join a little early and reconnect briefly.
# Set both to 0 if you want exact start/end only.
JOIN_GRACE_MINUTES = int(os.environ.get("MENTOR_CALL_JOIN_GRACE_MINUTES", "5"))
END_GRACE_MINUTES = int(os.environ.get("MENTOR_CALL_END_GRACE_MINUTES", "0"))


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
    day_of_week: str = Field(..., examples=["Monday"])
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

    if body.day_of_week not in VALID_DAYS:
        raise HTTPException(status_code=400, detail="Invalid day_of_week.")

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
                (mentor_id, day_of_week, start_time, end_time, slot_duration_minutes, is_active, created_at)
            VALUES (%s, %s, %s, %s, %s, TRUE, NOW())
            RETURNING *
            """,
            (db_user["id"], body.day_of_week, start_t, end_t, body.slot_duration_minutes),
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
    day_name = session_date.strftime("%A")

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
              AND day_of_week = %s
              AND is_active = TRUE
            ORDER BY start_time
            """,
            (mentor_id, day_name),
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
    body: BookSlotBody,
    current_user: dict = Depends(get_current_user),
):
    """Mentee books one slot with a mentor."""
    db_user = get_db_user_by_firebase_uid(current_user["uid"])
    require_role(db_user, "mentee")

    start_t = parse_hhmm(body.start_time)
    end_t = parse_hhmm(body.end_time)
    if start_t >= end_t:
        raise HTTPException(status_code=400, detail="start_time must be before end_time.")

    if datetime.combine(body.session_date, start_t) <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Cannot book a past slot.")

    day_name = body.session_date.strftime("%A")

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
                WHERE id = %s AND mentor_id = %s AND day_of_week = %s AND is_active = TRUE
                """,
                (body.availability_id, body.mentor_id, day_name),
            )
        else:
            cur.execute(
                """
                SELECT * FROM mentor_availability
                WHERE mentor_id = %s
                  AND day_of_week = %s
                  AND start_time <= %s
                  AND end_time >= %s
                  AND is_active = TRUE
                ORDER BY start_time
                LIMIT 1
                """,
                (body.mentor_id, day_name, start_t, end_t),
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
        conn.commit()
        return {"status": "booked", "booking": booking}

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
