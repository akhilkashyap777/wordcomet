from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional, Literal
from datetime import datetime

router = APIRouter(prefix="/friends", tags=["Friends"])

# ─── LOCAL MOCK DATA (Simulating Postgres) ───────────────────────────
MOCK_USERS = {
    1: {"id": 1, "display_name": "Alice", "email": "alice@test.com", "role": "mentee"},
    2: {"id": 2, "display_name": "Bob", "email": "bob@test.com", "role": "mentor"},
    3: {"id": 3, "display_name": "Charlie", "email": "charlie@test.com", "role": "mentor"},
}

# This simulates your "friendships" table records
MOCK_FRIENDSHIPS = [] 
friendship_id_counter = 1

# ─── Pydantic Models ────────────────────────────────────────────────

class FriendRequestInput(BaseModel):
    to_user_id: int

class FriendResponseInput(BaseModel):
    request_id: int
    action: Literal["accept", "decline", "block"]

class FriendUserOut(BaseModel):
    friendship_id: int
    user_id: int
    display_name: str
    email: str
    role: Optional[str]
    status: str

# ─── Local Auth Helper (Simulating Firebase Token Decoding) ─────────
def get_mock_current_user(x_test_user_id: int = Header(..., description="Simulate logging in as User ID (1, 2, or 3)")):
    if x_test_user_id not in MOCK_USERS:
        raise HTTPException(status_code=401, detail="Unauthorized Mock User. Use 1, 2, or 3.")
    return MOCK_USERS[x_test_user_id]

# ─── Endpoints ───────────────────────────────────────────────────────

@router.post("/request", status_code=201)
def send_friend_request(
    body: FriendRequestInput, 
    x_test_user_id: int = Header(..., description="Your simulated user ID")
):
    global friendship_id_counter
    current_user = get_mock_current_user(x_test_user_id)
    from_id = current_user["id"]
    to_id = body.to_user_id

    if from_id == to_id:
        raise HTTPException(status_code=400, detail="You cannot send a friend request to yourself.")
    if to_id not in MOCK_USERS:
        raise HTTPException(status_code=404, detail="Target user does not exist.")

    # Check for existing request/friendship
    for f in MOCK_FRIENDSHIPS:
        if (f["from_user_id"] == from_id and f["to_user_id"] == to_id) or \
           (f["from_user_id"] == to_id and f["to_user_id"] == from_id):
            
            if f["status"] == "accepted":
                raise HTTPException(status_code=400, detail="You are already friends.")
            elif f["status"] == f"pending":
                if f["from_user_id"] == from_id:
                    raise HTTPException(status_code=400, detail="Friend request already pending.")
                else:
                    raise HTTPException(status_code=400, detail="This user already sent you a request. Accept it.")
            elif f["status"] == "blocked":
                raise HTTPException(status_code=403, detail="Action blocked.")

    # Insert request
    new_request = {
        "id": friendship_id_counter,
        "from_user_id": from_id,
        "to_user_id": to_id,
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    MOCK_FRIENDSHIPS.append(new_request)
    friendship_id_counter += 1
    return {"status": "success", "message": f"Request sent from {current_user['display_name']} to user {to_id}."}


@router.post("/respond")
def respond_to_friend_request(
    body: FriendResponseInput,
    x_test_user_id: int = Header(..., description="Your simulated user ID")
):
    current_user = get_mock_current_user(x_test_user_id)
    user_id = current_user["id"]

    target_req = None
    for f in MOCK_FRIENDSHIPS:
        if f["id"] == body.request_id:
            target_req = f
            break

    if not target_req:
        raise HTTPException(status_code=404, detail="Friend request record not found.")
    if target_req["to_user_id"] != user_id:
        raise HTTPException(status_code=403, detail="This request was not sent to you.")
    if target_req["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Request is already {target_req['status']}.")

    if body.action == "accept":
        target_req["status"] = "accepted"
        return {"status": "success", "message": "Friend request accepted!"}
    elif body.action == "decline":
        MOCK_FRIENDSHIPS.remove(target_req)
        return {"status": "success", "message": "Friend request declined."}


@router.get("/pending", response_model=List[FriendUserOut])
def get_pending_requests(x_test_user_id: int = Header(..., description="Your simulated user ID")):
    current_user = get_mock_current_user(x_test_user_id)
    user_id = current_user["id"]
    
    results = []
    for f in MOCK_FRIENDSHIPS:
        if f["to_user_id"] == user_id and f["status"] == "pending":
            sender = MOCK_USERS[f["from_user_id"]]
            results.append({
                "friendship_id": f["id"],
                "user_id": sender["id"],
                "display_name": sender["display_name"],
                "email": sender["email"],
                "role": sender["role"],
                "status": f["status"]
            })
    return results


@router.get("/list", response_model=List[FriendUserOut])
def get_friends_list(x_test_user_id: int = Header(..., description="Your simulated user ID")):
    current_user = get_mock_current_user(x_test_user_id)
    user_id = current_user["id"]
    
    results = []
    for f in MOCK_FRIENDSHIPS:
        if f["status"] == "accepted" and (f["from_user_id"] == user_id or f["to_user_id"] == user_id):
            friend_id = f["to_user_id"] if f["from_user_id"] == user_id else f["from_user_id"]
            friend = MOCK_USERS[friend_id]
            results.append({
                "friendship_id": f["id"],
                "user_id": friend["id"],
                "display_name": friend["display_name"],
                "email": friend["email"],
                "role": friend["role"],
                "status": f["status"]
            })
    return results