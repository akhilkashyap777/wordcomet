import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

FCM_TOKENS_FILE = os.path.join(BASE_DIR, "fcm_tokens.json")
FCM_COUNTRIES_FILE = os.path.join(BASE_DIR, "fcm_countries.json")

try:
    with open(FCM_TOKENS_FILE, "r") as file:
        fcm_tokens = set(json.load(file))
except (FileNotFoundError, json.JSONDecodeError):
    fcm_tokens = set()

try:
    with open(FCM_COUNTRIES_FILE, "r") as file:
        fcm_countries = json.load(file)
except (FileNotFoundError, json.JSONDecodeError):
    fcm_countries = {}


def _save_tokens(tokens=None):
    tokens_to_save = tokens if tokens is not None else fcm_tokens

    with open(FCM_TOKENS_FILE, "w") as file:
        json.dump(list(tokens_to_save), file)


def _save_countries():
    with open(FCM_COUNTRIES_FILE, "w") as file:
        json.dump(fcm_countries, file, indent=2)