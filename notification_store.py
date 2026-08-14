import json
import os

FCM_TOKENS_FILE = "fcm_tokens.json"

try:
    with open(FCM_TOKENS_FILE, "r") as file:
        fcm_tokens = json.load(file)
except (FileNotFoundError, json.JSONDecodeError):
    fcm_tokens = {}


def _save_tokens():
    with open(FCM_TOKENS_FILE, "w") as file:
        json.dump(fcm_tokens, file)