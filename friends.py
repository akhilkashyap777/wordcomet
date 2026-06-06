from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import datetime
# Assuming you have a database connection pool or session manager import:
# from database import db_session 
# Assuming you have an auth helper that validates the token and returns the DB user row:
from auth import get_current_user
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/friends", tags=["Friends"])

class DatabaseSession:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST"),
            port=os.getenv("DB_PORT"),
            database=os.getenv("DB_NAME")
        )

    async def fetch_row(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetch_all(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def execute(self, query, *args):
        async with self.pool.acquire() as connection:
            return await connection.execute(query, *args)

    async def disconnect(self):
        if self.pool:
            await self.pool.close()
            self.pool = None

# Global instance to import in your routes
db_session = DatabaseSession()


# ─── Pydantic Models ────────────────────────────────────────────────

class FriendRequestInput(BaseModel):
    to_user_id: int  # The database internal bigint ID of the target user

class FriendResponseInput(BaseModel):
    request_id: int
    action: Literal["accept", "decline", "block"]

class FriendUserOut(BaseModel):
    friendship_id: int
    user_id: int
    display_name: str
    email: str
    profile_picture_url: Optional[str]
    role: Optional[str]
    status: str

# ─── Endpoints ───────────────────────────────────────────────────────

@router.post("/request", status_code=201)
async def send_friend_request(
    body: FriendRequestInput, 
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user) # Returns your logged-in DB user row
):
    """
    Send a friend request to another user by their database ID.
    """

    firebase_uid = current_user["uid"]

    user = await db_session.fetch_row(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid
    )

    if not user:
        raise HTTPException(status_code=404, detail="Logged-in user not found in database")

    from_id = user["id"]

    to_id = body.to_user_id

    if from_id == to_id:
        raise HTTPException(status_code=400, detail="You cannot send a friend request to yourself.")

    # 1. Check if a relationship already exists in either direction
    # To handle constraints correctly, we check both combinations
    existing_query = """
        SELECT id, from_user_id, to_user_id, status 
        FROM friendships 
        WHERE (from_user_id = $1 AND to_user_id = $2) 
           OR (from_user_id = $2 AND to_user_id = $1)
    """
    relationship = await db_session.fetch_row(existing_query, from_id, to_id)
    relationship = None # Placeholder for execution logic
    
    if relationship:
        status = relationship["status"]
        if status == "accepted":
            raise HTTPException(status_code=400, detail="You are already friends with this user.")
        elif status == "pending":
            if relationship["from_user_id"] == from_id:
                raise HTTPException(status_code=400, detail="Friend request already sent and pending.")
            else:
                raise HTTPException(status_code=400, detail="This user has already sent you a request. Accept that instead.")
        elif status == "blocked":
            raise HTTPException(status_code=403, detail="Action not allowed.")

    print("FROM ID:", from_id)
    print("TO ID:", to_id)

    # 2. Insert the pending friend request
    insert_query = """
        INSERT INTO friendships (from_user_id, to_user_id, status, created_at)
        VALUES ($1, $2, 'pending', now())
        RETURNING id;
    """
    new_id = await db_session.fetch_row(insert_query, from_id, to_id)

    # 3. Trigger a push notification via background tasks if FCM token exists for target user
    # background_tasks.add_task(notify_friend_request, to_id, current_user["display_name"])

    return {"status": "success", "message": "Friend request sent successfully."}


@router.post("/respond")
async def respond_to_friend_request(
    body: FriendResponseInput,
    current_user: dict = Depends(get_current_user)
):
    """
    Accept, Decline, or Block a friend request.
    """
    firebase_uid = current_user["uid"]

    user = await db_session.fetch_row(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid
    )

    if not user:
        raise HTTPException(status_code=404, detail="Logged-in user not found")

    user_id = user["id"]

    # Fetch request to ensure the logged-in user is actually the recipient
    query = "SELECT * FROM friendships WHERE id = $1"
    # request_row = await db_session.fetch_row(query, body.request_id)
    request_row = await db_session.fetch_row(query, body.request_id)

    if not request_row:
        raise HTTPException(status_code=404, detail="Friend request not found.")

    if request_row["to_user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to respond to this request.")
    
    if request_row["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {request_row['status']}.")

    if body.action == "accept":
        update_query = """
            UPDATE friendships 
            SET status = 'accepted', accepted_at = now() 
            WHERE id = $1
        """
        await db_session.execute(update_query, body.request_id)
        return {"status": "success", "message": "Friend request accepted."}

    elif body.action == "decline":
        # Delete row if declined to allow them to try requesting again in future
        delete_query = "DELETE FROM friendships WHERE id = $1"
        # await db_session.execute(delete_query, body.request_id)
        return {"status": "success", "message": "Friend request declined."}

    elif body.action == "block":
        update_query = """
            UPDATE friendships 
            SET status = 'blocked', accepted_at = NULL 
            WHERE id = $1
        """
        # await db_session.execute(update_query, body.request_id)
        return {"status": "success", "message": "User has been blocked."}


@router.get("/pending", response_model=List[FriendUserOut])
async def get_pending_requests(current_user: dict = Depends(get_current_user)):
    firebase_uid = current_user["uid"]

    user = await db_session.fetch_row(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid
    )

    if not user:
        raise HTTPException(status_code=404, detail="Logged-in user not found")

    user_id = user["id"]

    query = """
        SELECT 
            f.id as friendship_id,
            u.id as user_id,
            COALESCE(u.full_name, u.display_name, u.email, 'Unknown User') AS display_name,
            u.email,
            u.profile_picture_url,
            u.role,
            f.status
        FROM friendships f
        JOIN users u ON f.from_user_id = u.id
        WHERE f.to_user_id = $1 
          AND f.status = 'pending'
        ORDER BY f.created_at DESC
    """

    rows = await db_session.fetch_all(query, user_id)
    return [dict(row) for row in rows]


@router.get("/list", response_model=List[FriendUserOut])
async def get_friends_list(current_user: dict = Depends(get_current_user)):
    firebase_uid = current_user["uid"]

    user = await db_session.fetch_row(
        "SELECT id FROM users WHERE firebase_uid = $1",
        firebase_uid
    )

    if not user:
        raise HTTPException(status_code=404, detail="Logged-in user not found")

    user_id = user["id"]

    query = """
        SELECT 
            f.id as friendship_id,
            u.id as user_id,
            COALESCE(u.full_name, u.display_name, u.email, 'Unknown User') AS display_name,
            u.email,
            u.profile_picture_url,
            u.role,
            f.status
        FROM friendships f
        JOIN users u 
          ON u.id = CASE 
              WHEN f.from_user_id = $1 THEN f.to_user_id 
              ELSE f.from_user_id 
          END
        WHERE (f.from_user_id = $1 OR f.to_user_id = $1)
          AND f.status = 'accepted'
        ORDER BY f.accepted_at DESC
    """

    rows = await db_session.fetch_all(query, user_id)

    return [dict(row) for row in rows]

@router.get("/search")
async def search_users(
    q: str,
    current_user: dict = Depends(get_current_user)
):
    firebase_uid = current_user["user_id"]

    query = """
        SELECT
            id,
            display_name,
            full_name,
            profile_picture_url
        FROM users
        WHERE firebase_uid != $1
          AND (
                display_name ILIKE $2
             OR full_name ILIKE $2
             OR email ILIKE $2
          )
        ORDER BY display_name
        LIMIT 20
    """

    rows = await db_session.fetch_all(
        query,
        firebase_uid,
        f"%{q}%"
    )

    return [dict(row) for row in rows]
print("🔥 SEARCH ENDPOINT FILE LOADED")