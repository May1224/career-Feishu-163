"""Minimal Feishu group-webhook notification, without including mail bodies."""
import json
from urllib.request import Request, urlopen


def send(webhook, text):
    if not webhook:
        return
    payload = json.dumps({'msg_type': 'text', 'content': {'text': text}}, ensure_ascii=False).encode()
    try:
        with urlopen(Request(webhook, payload, {'Content-Type': 'application/json'}, method='POST'), timeout=15) as response:
            json.load(response)
    except Exception:
        # A notification failure must not turn a completed mail sync into a retry.
        return
