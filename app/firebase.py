import os
import json
import firebase_admin
from firebase_admin import credentials, messaging

# Only initialize once
if not firebase_admin._apps:
    key_json = os.environ.get("FIREBASE_NOTIFICATION")
    if key_json:
        cred_dict = json.loads(key_json)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)

def send_push(token: str, title: str, body: str, data: dict = None):
    """Send push notification to a device token"""
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data=data or {}
    )
    try:
        response = messaging.send(message)
        print(f"Firebase message sent: {response}")
    except Exception as e:
        print(f"Firebase send failed: {e}")
