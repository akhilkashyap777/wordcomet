import uuid
import httpx
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
import os


router = APIRouter()

TURN_TOKEN_ID = os.environ.get("TURN_TOKEN_ID")
TURN_API_TOKEN = os.environ.get("TURN_API_TOKEN")


chat_rooms = {}


@router.get("/turn-credentials")
async def get_turn_credentials():
    """Generate short-lived TURN credentials for a client."""
    print("TURN_TOKEN_ID loaded:", bool(TURN_TOKEN_ID))
    print("TURN_API_TOKEN loaded:", bool(TURN_API_TOKEN))
    print("TURN_API_TOKEN length:", len(TURN_API_TOKEN or ""))
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://rtc.live.cloudflare.com/v1/turn/keys/{TURN_TOKEN_ID}/credentials/generate-ice-servers",
            headers={
                "Authorization": f"Bearer {TURN_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"ttl": 86400},
        )
        print("CLOUDFLARE STATUS:", response.status_code)
        print("CLOUDFLARE BODY:", response.text)

    print("STATUS:", response.status_code)
    print("BODY:", response.text)

    if response.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=response.text
        )

    return response.json()

@router.websocket("/ws/chat/{room_id}")
async def chat_websocket(websocket: WebSocket, room_id: str):
    # Validate booking and session timing
    cur.execute("""
        SELECT mentor_id, mentee_id, session_date,
            start_time, end_time
        FROM mentor_bookings
        WHERE video_room_id = %s
    """, (room_id,))

    booking = cur.fetchone()

    if not booking:
        await websocket.close()
        return

    now = datetime.now()

    session_start = datetime.combine(
        booking["session_date"],
        booking["start_time"]
    )

    session_end = datetime.combine(
        booking["session_date"],
        booking["end_time"]
    )

    if now < session_start or now > session_end:
        await websocket.close()
        return

    await websocket.accept()

    if room_id not in chat_rooms:
        chat_rooms[room_id] = []

    # Allow only two users in one video-call room
    if len(chat_rooms[room_id]) >= 2:
        await websocket.send_json({
            "type": "error",
            "message": "Room is full"
        })
        await websocket.close(code=4003)
        return

    chat_rooms[room_id].append(websocket)

    participant_count = len(chat_rooms[room_id])

    print(f"💬 User joined room {room_id}")
    print(f"💬 Participants: {participant_count}")

    # Tell this browser that it successfully joined
    await websocket.send_json({
        "type": "joined",
        "room": room_id,
        "participants": participant_count
    })

    # When the second user joins, select one user to create the offer
    if participant_count == 2:
        first_peer = chat_rooms[room_id][0]
        second_peer = chat_rooms[room_id][1]

        await first_peer.send_json({
            "type": "peer_ready",
            "should_create_offer": True
        })

        await second_peer.send_json({
            "type": "peer_ready",
            "should_create_offer": False
        })

    try:
        while True:
            data = await websocket.receive_text()

            # Forward offer, answer, ICE candidate, or any other message
            for client in list(chat_rooms.get(room_id, [])):
                if client != websocket:
                    await client.send_text(data)

    except WebSocketDisconnect:
        print(f"💬 User left room {room_id}")

    finally:
        if websocket in chat_rooms.get(room_id, []):
            chat_rooms[room_id].remove(websocket)

        # Inform the remaining user
        for client in chat_rooms.get(room_id, []):
            try:
                await client.send_json({
                    "type": "peer_left"
                })
            except Exception:
                pass

        if room_id in chat_rooms and not chat_rooms[room_id]:
            del chat_rooms[room_id]
    # await websocket.accept()

    # if room_id not in chat_rooms:
    #     chat_rooms[room_id] = []

    # chat_rooms[room_id].append(websocket)

    # print(f"💬 User joined chat room {room_id}")

    # try:
    #     while True:
    #         data = await websocket.receive_text()

    #         for client in chat_rooms[room_id]:
    #             if client != websocket:
    #                 await client.send_text(data)

    # except WebSocketDisconnect:
    #     print(f"💬 User left chat room {room_id}")

    #     if websocket in chat_rooms.get(room_id, []):
    #         chat_rooms[room_id].remove(websocket)

    #     if room_id in chat_rooms and not chat_rooms[room_id]:
    #         del chat_rooms[room_id]