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
import boto3
from fastapi import UploadFile, File, Form
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



# test credentials error showing up
# def get_db():
#     return psycopg2.connect(
#         dbname=os.environ.get("DB_NAME", "wordcomet_testenv"),
#         user=os.environ.get("DB_USER", "wordcomet_user"),
#         password=os.environ.get("DB_PASSWORD", ""),
#         host=os.environ.get("DB_HOST", "69.62.78.126"),
#         port=os.environ.get("DB_PORT", "5432"),
#         cursor_factory=psycopg2.extras.RealDictCursor,
#     )


# ─── Auth Helper ─────────────────────────────────────────────────────

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        print("TOKEN RECEIVED:")
        print(credentials.credentials[:50])

        decoded = fb_auth.verify_id_token(credentials.credentials)

        print("TOKEN VALID")
        print(decoded)

        return decoded

    except fb_auth.InvalidIdTokenError as e:
        print("INVALID TOKEN ERROR:", e)
        raise HTTPException(status_code=401, detail="Invalid or expired token.")

    except Exception as e:
        print("GENERAL AUTH ERROR:", e)
        raise HTTPException(status_code=401, detail=f"Unauthorized: {str(e)}")


# ─── Pydantic Models ─────────────────────────────────────────────────

VALID_ROLES = ("mentor", "mentee")

class SignupBody(BaseModel):
    id_token: str
    role: Optional[str] = None                               # "mentor" or "mentee"
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
    notification_time: Optional[str] = None
    notifications_enabled: Optional[bool] = None

    # mentor fields
    bio: Optional[str] = None

    # mentee fields
    learning_goal: Optional[str] = None

    age: Optional[int] = None
    gender: Optional[str] = None
    qualification: Optional[str] = None
    course: Optional[str] = None
    phone_number: Optional[str] = None

class RateMentorBody(BaseModel):
    mentor_id: int
    rating: int
    comment: Optional[str] = None

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

    print("Decoded Firebase token:", decoded)
    print("Firebase UID:", firebase_uid)
    print("Email:", email)

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
            "mentee",
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

    print("Decoded Firebase token:", decoded)
    print("Firebase UID:", firebase_uid)
    print("Email:", email)

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

        return {
            "status": "ok",
            "access_token": body.id_token,
            "token_type": "bearer",
            "role": updated_user["role"],
            "user": updated_user
        }

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

@router.patch("/profile/patch")
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

    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT role FROM users WHERE firebase_uid = %s",
        (firebase_uid,)
    )

    db_user = cur.fetchone()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found.")

    role = db_user["role"]

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
    if body.age is not None:
        fields["age"] = body.age

    if body.gender is not None:
        fields["gender"] = body.gender

    if body.qualification is not None:
        fields["qualification"] = body.qualification

    if body.course is not None:
        fields["course"] = body.course

    if body.phone_number is not None:
        fields["phone_number"] = body.phone_number

    if body.bio is not None:
        if role != "mentor":
            raise HTTPException(status_code=403, detail="Only mentors can update bio.")
        fields["bio"] = body.bio

    if body.learning_goal is not None:
        if role != "mentee":
            raise HTTPException(status_code=403, detail="Only mentees can update learning_goal.")
        fields["learning_goal"] = body.learning_goal

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

@router.post("/mentors/rate")
def rate_mentor(
    body: RateMentorBody,
    current_user: dict = Depends(get_current_user)
):
    firebase_uid = current_user["uid"]

    if body.rating < 1 or body.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5.")

    conn = get_db()
    try:
        cur = conn.cursor()

        # logged-in user must be mentee
        cur.execute("SELECT id, role FROM users WHERE firebase_uid = %s", (firebase_uid,))
        mentee = cur.fetchone()

        if not mentee:
            raise HTTPException(status_code=404, detail="Mentee not found.")

        if mentee["role"] != "mentee":
            raise HTTPException(status_code=403, detail="Only mentees can rate mentors.")

        # target user must be mentor
        cur.execute("SELECT id, role FROM users WHERE id = %s", (body.mentor_id,))
        mentor = cur.fetchone()

        if not mentor:
            raise HTTPException(status_code=404, detail="Mentor not found.")

        if mentor["role"] != "mentor":
            raise HTTPException(status_code=400, detail="You can only rate mentors.")

        # insert or update rating
        cur.execute("""
            INSERT INTO mentor_reviews (mentor_id, mentee_id, rating, comment)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (mentor_id, mentee_id)
            DO UPDATE SET
                rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                created_at = NOW()
            RETURNING *
        """, (
            body.mentor_id,
            mentee["id"],
            body.rating,
            body.comment
        ))

        review = dict(cur.fetchone())

        # update mentor average
        cur.execute("""
            UPDATE users
            SET
                average_rating = (
                    SELECT ROUND(AVG(rating)::numeric, 2)
                    FROM mentor_reviews
                    WHERE mentor_id = %s
                ),
                rating_count = (
                    SELECT COUNT(*)
                    FROM mentor_reviews
                    WHERE mentor_id = %s
                ),
                updated_at = NOW()
            WHERE id = %s
            RETURNING id, display_name, average_rating, rating_count
        """, (
            body.mentor_id,
            body.mentor_id,
            body.mentor_id
        ))

        mentor_rating = dict(cur.fetchone())

        conn.commit()

        return {
            "status": "rated",
            "review": review,
            "mentor": mentor_rating
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Rating failed: {str(e)}")
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
                email,
                age,
                gender,
                qualification,
                course,
                phone_number,
                role,
                preferred_language,
                bio,
                average_rating,
                rating_count,
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

@router.get("/mentors")
def list_mentors(current_user: dict = Depends(get_current_user)):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                id,
                display_name,
                full_name,
                profile_picture_url,
                bio,
                average_rating,
                rating_count
            FROM users
            WHERE role = 'mentor'
              AND is_active = TRUE
            ORDER BY average_rating DESC NULLS LAST, display_name ASC
        """)
        return {"mentors": [dict(row) for row in cur.fetchall()]}
    finally:
        conn.close()

@router.put("/profile/update")
def replace_my_profile(
    body: UpdateProfileBody,
    current_user: dict = Depends(get_current_user)
):
    """
    Full profile update using PUT.
    Replaces all provided profile fields.
    """

    firebase_uid = current_user["uid"]

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT role FROM users WHERE firebase_uid = %s",
            (firebase_uid,)
        )

        db_user = cur.fetchone()

        if not db_user:
            raise HTTPException(status_code=404, detail="User not found.")

        role = db_user["role"]

        update_data = {
            "display_name": body.display_name,
            "full_name": body.full_name,
            "profile_picture_url": body.profile_picture_url,
            "preferred_language": body.preferred_language,
            "timezone": body.timezone,
            "notification_time": body.notification_time,
            "notifications_enabled": body.notifications_enabled,
            "age": body.age,
            "gender": body.gender,
            "qualification": body.qualification,
            "course": body.course,
            "phone_number": body.phone_number,
        }

        if role == "mentor":
            update_data["bio"] = body.bio

        if role == "mentee":
            update_data["learning_goal"] = body.learning_goal

        update_data["updated_at"] = datetime.now(timezone.utc)

        set_clause = ", ".join(f"{k} = %s" for k in update_data.keys())

        cur.execute(
            f"""
            UPDATE users
            SET {set_clause}
            WHERE firebase_uid = %s
            RETURNING *
            """,
            list(update_data.values()) + [firebase_uid]
        )

        updated = cur.fetchone()

        conn.commit()

        updated = dict(updated)
        updated.pop("fcm_token", None)
        updated.pop("device_id", None)

        return {
            "status": "updated",
            "user": updated
        }

    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Profile update failed: {str(e)}")

    finally:
        conn.close()

@router.post("/profile/create", status_code=201)
def create_my_profile(
    body: UpdateProfileBody,
    current_user: dict = Depends(get_current_user)
):
    """
    Create/complete logged-in user's profile after signup.
    Headers: Authorization: Bearer <firebase_id_token>
    """

    firebase_uid = current_user["uid"]

    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT id, role FROM users WHERE firebase_uid = %s",
            (firebase_uid,)
        )
        db_user = cur.fetchone()

        if not db_user:
            raise HTTPException(status_code=404, detail="User not found. Please signup first.")

        role = db_user["role"]

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

        if body.age is not None:
            fields["age"] = body.age

        if body.gender is not None:
            fields["gender"] = body.gender

        if body.qualification is not None:
            fields["qualification"] = body.qualification

        if body.course is not None:
            fields["course"] = body.course

        if body.phone_number is not None:
            fields["phone_number"] = body.phone_number


        if body.bio is not None:
            if role != "mentor":
                raise HTTPException(status_code=403, detail="Only mentors can add bio.")
            fields["bio"] = body.bio

        if body.learning_goal is not None:
            if role != "mentee":
                raise HTTPException(status_code=403, detail="Only mentees can add learning_goal.")
            fields["learning_goal"] = body.learning_goal

        if not fields:
            raise HTTPException(status_code=400, detail="No profile fields provided.")

        fields["updated_at"] = datetime.now(timezone.utc)

        set_clause = ", ".join(f"{k} = %s" for k in fields.keys())

        cur.execute(
            f"""
            UPDATE users
            SET {set_clause}
            WHERE firebase_uid = %s
            RETURNING *
            """,
            list(fields.values()) + [firebase_uid]
        )

        user = dict(cur.fetchone())
        conn.commit()

        user.pop("fcm_token", None)
        user.pop("device_id", None)

        return {
            "status": "created",
            "message": "Profile created/completed successfully.",
            "user": user
        }

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Profile creation failed: {str(e)}")
    finally:
        conn.close()


@router.get("/mentor/designations")
def get_mentor_designations():
    """
    Returns available mentor designations grouped by professional field.
    Used for mentor profile dropdown selection.
    """

    designations = {
        "Software Development": [
            "Software Engineer",
            "Senior Software Engineer",
            "Backend Developer",
            "Frontend Developer",
            "Full Stack Developer",
            "Python Developer",
            "Java Developer",
            ".NET Developer",
            "PHP Developer",
            "Node.js Developer",
            "React Developer",
            "Angular Developer",
            "Flutter Developer",
            "Android Developer",
            "iOS Developer",
            "Mobile App Developer",
            "Game Developer",
            "Embedded Software Engineer",
            "Application Developer",
            "Software Architect",
            "Technical Lead",
            "Engineering Manager"
        ],

        "Data Science and AI": [
            "Data Scientist",
            "Senior Data Scientist",
            "Machine Learning Engineer",
            "AI Engineer",
            "Deep Learning Engineer",
            "NLP Engineer",
            "Computer Vision Engineer",
            "Generative AI Engineer",
            "LLM Engineer",
            "Prompt Engineer",
            "MLOps Engineer",
            "AI Research Engineer",
            "Data Analyst",
            "Business Intelligence Analyst",
            "Research Scientist"
        ],

        "Data Engineering": [
            "Data Engineer",
            "Senior Data Engineer",
            "Big Data Engineer",
            "ETL Developer",
            "Data Warehouse Engineer",
            "Database Developer",
            "Database Administrator",
            "Analytics Engineer"
        ],

        "Cloud and DevOps": [
            "DevOps Engineer",
            "Senior DevOps Engineer",
            "Cloud Engineer",
            "AWS Engineer",
            "Azure Engineer",
            "Google Cloud Engineer",
            "Cloud Architect",
            "Site Reliability Engineer",
            "Platform Engineer",
            "Infrastructure Engineer",
            "Kubernetes Engineer",
            "Release Engineer",
            "Build Engineer"
        ],

        "Cybersecurity": [
            "Cybersecurity Analyst",
            "Security Engineer",
            "Application Security Engineer",
            "Cloud Security Engineer",
            "Network Security Engineer",
            "SOC Analyst",
            "Penetration Tester",
            "Ethical Hacker",
            "Security Consultant",
            "Security Architect",
            "Incident Response Analyst",
            "Digital Forensics Analyst",
            "GRC Analyst"
        ],

        "Testing and Quality Assurance": [
            "QA Engineer",
            "Software Test Engineer",
            "Automation Test Engineer",
            "Manual Test Engineer",
            "Performance Test Engineer",
            "SDET",
            "QA Lead",
            "Test Manager"
        ],

        "Product and Project Management": [
            "Product Manager",
            "Senior Product Manager",
            "Technical Product Manager",
            "Product Owner",
            "Project Manager",
            "Technical Project Manager",
            "Program Manager",
            "Scrum Master",
            "Delivery Manager"
        ],

        "UI UX and Design": [
            "UI Designer",
            "UX Designer",
            "UI/UX Designer",
            "Product Designer",
            "Graphic Designer",
            "Interaction Designer",
            "Visual Designer",
            "UX Researcher"
        ],

        "Networking and IT": [
            "Network Engineer",
            "Network Administrator",
            "System Administrator",
            "Linux Administrator",
            "Windows Administrator",
            "IT Support Engineer",
            "Technical Support Engineer",
            "IT Administrator",
            "Infrastructure Specialist"
        ],

        "Business and Management": [
            "Business Analyst",
            "Senior Business Analyst",
            "Management Consultant",
            "Business Consultant",
            "Operations Manager",
            "Strategy Consultant",
            "Business Development Manager",
            "Account Manager"
        ],

        "Finance and Accounting": [
            "Accountant",
            "Chartered Accountant",
            "Financial Analyst",
            "Investment Analyst",
            "Equity Research Analyst",
            "Risk Analyst",
            "Credit Analyst",
            "Auditor",
            "Tax Consultant",
            "Finance Manager",
            "Investment Banker"
        ],

        "Human Resources": [
            "HR Executive",
            "HR Manager",
            "HR Business Partner",
            "Talent Acquisition Specialist",
            "Recruiter",
            "Technical Recruiter",
            "Learning and Development Manager"
        ],

        "Marketing and Sales": [
            "Digital Marketing Specialist",
            "Marketing Manager",
            "SEO Specialist",
            "SEM Specialist",
            "Social Media Manager",
            "Content Marketing Manager",
            "Brand Manager",
            "Sales Executive",
            "Sales Manager",
            "Business Development Executive"
        ],

        "Mechanical Engineering": [
            "Mechanical Engineer",
            "Design Engineer",
            "Production Engineer",
            "Manufacturing Engineer",
            "Maintenance Engineer",
            "Quality Engineer",
            "Automotive Engineer",
            "HVAC Engineer",
            "CAD Engineer"
        ],

        "Civil Engineering": [
            "Civil Engineer",
            "Structural Engineer",
            "Site Engineer",
            "Construction Engineer",
            "Planning Engineer",
            "Quantity Surveyor",
            "Project Engineer",
            "Geotechnical Engineer"
        ],

        "Electrical and Electronics": [
            "Electrical Engineer",
            "Electronics Engineer",
            "Power Systems Engineer",
            "Control Systems Engineer",
            "Instrumentation Engineer",
            "Embedded Systems Engineer",
            "VLSI Engineer",
            "PCB Design Engineer"
        ],

        "Healthcare": [
            "Doctor",
            "Nurse",
            "Pharmacist",
            "Clinical Research Associate",
            "Medical Representative",
            "Healthcare Administrator",
            "Medical Coder",
            "Medical Lab Technician"
        ],

        "Education": [
            "Teacher",
            "Lecturer",
            "Assistant Professor",
            "Professor",
            "Academic Counselor",
            "Instructional Designer",
            "Corporate Trainer"
        ],

        "Legal": [
            "Lawyer",
            "Legal Associate",
            "Legal Consultant",
            "Corporate Lawyer",
            "Compliance Officer",
            "Legal Advisor"
        ],

        "Architecture": [
            "Architect",
            "Interior Designer",
            "Urban Planner",
            "Landscape Architect",
            "BIM Engineer"
        ],

        "Supply Chain and Logistics": [
            "Supply Chain Analyst",
            "Supply Chain Manager",
            "Logistics Coordinator",
            "Logistics Manager",
            "Procurement Specialist",
            "Procurement Manager",
            "Warehouse Manager"
        ],

        "Customer Service": [
            "Customer Support Executive",
            "Customer Success Manager",
            "Customer Service Representative",
            "Technical Support Specialist"
        ]
    }

    return {
        "status": "ok",
        "designations": designations
    }

def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )

@router.patch("/mentor/profile/update")
def update_mentor_professional_profile(
    designation: str = Form(...),
    experience_years: int = Form(...),
    experience_months: int = Form(...),
    resume: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    firebase_uid = current_user["uid"]

    if experience_years < 0:
        raise HTTPException(
            status_code=400,
            detail="experience_years cannot be negative."
        )

    if experience_months < 0 or experience_months > 11:
        raise HTTPException(
            status_code=400,
            detail="experience_months must be between 0 and 11."
        )

    if resume.content_type not in [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ]:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, DOC, or DOCX resumes are allowed."
        )

    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT id, role
            FROM users
            WHERE firebase_uid = %s
              AND is_active = TRUE
            """,
            (firebase_uid,)
        )

        user = cur.fetchone()

        if not user:
            raise HTTPException(
                status_code=404,
                detail="User not found."
            )

        if user["role"] != "mentor":
            raise HTTPException(
                status_code=403,
                detail="Only mentors can update mentor professional details."
            )

        bucket_name = os.environ.get("R2_BUCKET_NAME")
        folder = os.environ.get(
            "R2_MENTOR_RESUME_FOLDER",
            "mentor_resumes"
        )

        if not bucket_name:
            raise HTTPException(
                status_code=500,
                detail="R2_BUCKET_NAME missing in env."
            )

        file_ext = resume.filename.split(".")[-1].lower()

        resume_key = (
            f"{folder}/mentor_{user['id']}/resume.{file_ext}"
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
            UPDATE users
            SET
                designation = %s,
                experience_years = %s,
                experience_months = %s,
                mentor_resume_key = %s,
                mentor_resume_filename = %s,
                mentor_resume_uploaded_at = NOW(),
                updated_at = NOW()
            WHERE firebase_uid = %s
            RETURNING
                id,
                display_name,
                role,
                designation,
                experience_years,
                experience_months,
                mentor_resume_key,
                mentor_resume_filename,
                mentor_resume_uploaded_at
            """,
            (
                designation,
                experience_years,
                experience_months,
                resume_key,
                resume.filename,
                firebase_uid,
            )
        )

        updated = dict(cur.fetchone())

        conn.commit()

        return {
            "status": "updated",
            "mentor": updated
        }

    except HTTPException:
        conn.rollback()
        raise

    except Exception as e:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Mentor profile update failed: {str(e)}"
        )

    finally:
        conn.close()