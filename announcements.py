import os

import firebase_admin
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from firebase_admin import messaging
from pydantic import BaseModel
from typing import Optional

from notification_store import (
    fcm_tokens,
    fcm_countries,
    _save_tokens,
    _save_countries,
)


router = APIRouter(tags=["Announcements"])

ADMIN_API_KEY = os.environ.get("WORDCOMET_ADMIN_KEY")


class AnnouncementInput(BaseModel):
    title: str
    matter: str
    image: Optional[str] = None
    country: str = "ALL"


def send_announcement(
    title: str,
    matter: str,
    image: Optional[str] = None,
    country: str = "ALL"
):
    if not firebase_admin._apps:
        print("Firebase is not initialized.")
        return

    country = country.upper()
    stale_tokens = set()
    success_count = 0
    failed_count = 0

    if country == "ALL":
        selected_tokens = list(fcm_tokens)
    else:
        selected_tokens = [
            token
            for token in fcm_tokens
            if fcm_countries.get(token) == country
        ]

    for token in selected_tokens:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=matter,
                image=image
            ),
            data={
                "type": "announcement",
                "title": title,
                "matter": matter,
                "image": image or "",
                "country": country
            },
            token=token
        )

        try:
            messaging.send(message)
            success_count += 1
        except messaging.UnregisteredError:
            stale_tokens.add(token)
        except Exception as error:
            failed_count += 1
            print(f"Announcement failed: {error}")

    if stale_tokens:
        fcm_tokens.difference_update(stale_tokens)

        for token in stale_tokens:
            fcm_countries.pop(token, None)

        _save_tokens(fcm_tokens)
        _save_countries()

    print(
        f"Announcement country={country}, "
        f"selected={len(selected_tokens)}, "
        f"sent={success_count}, "
        f"failed={failed_count}"
    )


@router.post("/admin/announcement")
def create_announcement(
    body: AnnouncementInput,
    background_tasks: BackgroundTasks,
    x_api_key: str = Header(...)
):
    if not ADMIN_API_KEY or x_api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

    country = body.country.strip().upper()

    if country != "ALL" and len(country) != 2:
        raise HTTPException(
            status_code=400,
            detail="Country must be ALL or a two-letter code such as IN or US"
        )

    background_tasks.add_task(
        send_announcement,
        body.title,
        body.matter,
        body.image,
        country
    )

    return {
        "status": "accepted",
        "message": "Announcement is being sent"
    }