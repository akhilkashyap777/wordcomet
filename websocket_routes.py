import uuid
import httpx

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
import os


router = APIRouter()

TURN_TOKEN_ID = os.environ.get("TURN_TOKEN_ID")
TURN_API_TOKEN = os.environ.get("TURN_API_TOKEN")


# Queues for waiting teachers and students
waiting_teachers: list[WebSocket] = []
waiting_students: list[WebSocket] = []

# Active rooms: room_id → {teacher: WebSocket, student: WebSocket}
active_rooms: dict[str, dict] = {}

# Track which room each websocket belongs to
socket_to_room: dict[WebSocket, str] = {}

# Track role of each socket
socket_to_role: dict[WebSocket, str] = {}

chat_rooms = {}

async def try_pair():
    """If there's at least one teacher and one student waiting, pair them."""
    if waiting_teachers and waiting_students:
        teacher = waiting_teachers.pop(0)
        student = waiting_students.pop(0)

        room_id = str(uuid.uuid4())
        active_rooms[room_id] = {"teacher": teacher, "student": student}
        socket_to_room[teacher] = room_id
        socket_to_room[student] = room_id

        await teacher.send_text(
            f'{{"type":"paired","role":"teacher","room_id":"{room_id}"}}'
        )
        await student.send_text(
            f'{{"type":"paired","role":"student","room_id":"{room_id}"}}'
        )

        print(f"✅ Paired teacher + student in room {room_id}")


async def put_back_in_queue(websocket: WebSocket, role: str):
    """Put a peer back in queue after their partner disconnected."""
    try:
        await websocket.send_text(
            f'{{"type":"waiting","role":"{role}","reason":"partner_left"}}'
        )

        if role == "teacher":
            waiting_teachers.append(websocket)
            print(f"👨‍🏫 Teacher back in queue — {len(waiting_teachers)} waiting")
        else:
            waiting_students.append(websocket)
            print(f"👨‍🎓 Student back in queue — {len(waiting_students)} waiting")

    except Exception:
        pass


@router.get("/turn-credentials")
async def get_turn_credentials():
    """Generate short-lived TURN credentials for a client."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://rtc.live.cloudflare.com/v1/turn/keys/{TURN_TOKEN_ID}/credentials/generate-ice-servers",
            headers={
                "Authorization": f"Bearer {TURN_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"ttl": 86400},
        )

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to get TURN credentials.")

    return response.json()


@router.websocket("/ws/signal")
async def signaling_endpoint(websocket: WebSocket, role: str = "student"):
    """
    Connect as:
    ws://localhost:8011/ws/signal?role=teacher
    ws://localhost:8011/ws/signal?role=student
    """

    if role not in ("teacher", "student"):
        await websocket.close(code=1008, reason="role must be teacher or student")
        return

    if room_id not in chat_rooms:
        chat_rooms[room_id] = []

    if len(chat_rooms[room_id]) >= 2:
        await websocket.close(code=1008, reason="Room full")
        return

    await websocket.accept()
    socket_to_role[websocket] = role

    if role == "teacher":
        waiting_teachers.append(websocket)
        print(f"👨‍🏫 Teacher joined queue — {len(waiting_teachers)} waiting")
    else:
        waiting_students.append(websocket)
        print(f"👨‍🎓 Student joined queue — {len(waiting_students)} waiting")

    await websocket.send_text(f'{{"type":"waiting","role":"{role}"}}')

    await try_pair()

    try:
        while True:
            data = await websocket.receive_text()
            room_id = socket_to_room.get(websocket)

            if not room_id:
                continue

            if room_id not in active_rooms:
                continue

            room = active_rooms[room_id]
            peer = room["student"] if websocket == room["teacher"] else room["teacher"]

            try:
                await peer.send_text(data)
            except Exception:
                pass

    except WebSocketDisconnect:
        print(f"🔌 {role} disconnected")

        room_id = socket_to_room.pop(websocket, None)
        socket_to_role.pop(websocket, None)

        if room_id and room_id in active_rooms:
            room = active_rooms.pop(room_id)

            peer = room["student"] if websocket == room["teacher"] else room["teacher"]
            peer_role = "student" if websocket == room["teacher"] else "teacher"

            socket_to_room.pop(peer, None)

            print(f"👋 Room {room_id} closed — putting {peer_role} back in queue")

            await put_back_in_queue(peer, peer_role)
            await try_pair()

        else:
            if websocket in waiting_teachers:
                waiting_teachers.remove(websocket)
                print(f"👨‍🏫 Teacher left queue — {len(waiting_teachers)} remaining")

            if websocket in waiting_students:
                waiting_students.remove(websocket)
                print(f"👨‍🎓 Student left queue — {len(waiting_students)} remaining")

@router.websocket("/ws/chat/{room_id}")
async def chat_websocket(websocket: WebSocket, room_id: str):
    await websocket.accept()

    if room_id not in chat_rooms:
        chat_rooms[room_id] = []

    chat_rooms[room_id].append(websocket)

    print(f"💬 User joined chat room {room_id}")

    try:
        while True:
            data = await websocket.receive_text()

            for client in chat_rooms[room_id]:
                if client != websocket:
                    await client.send_text(data)

    except WebSocketDisconnect:
        print(f"💬 User left chat room {room_id}")

        if websocket in chat_rooms.get(room_id, []):
            chat_rooms[room_id].remove(websocket)

        if room_id in chat_rooms and not chat_rooms[room_id]:
            del chat_rooms[room_id]