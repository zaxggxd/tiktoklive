"""
TikTok Live Notifier
---------------------
เช็คว่ารายชื่อ TikTok user ใน usernames.txt กำลังไลฟ์อยู่หรือไม่
ถ้าเจอว่า "เพิ่งเริ่มไลฟ์" (จากไม่ไลฟ์ -> ไลฟ์) จะส่ง Telegram แจ้งเตือน
เก็บสถานะล่าสุดไว้ใน state.json เพื่อไม่ให้แจ้งซ้ำ

ต้องตั้ง environment variables:
  TELEGRAM_TOKEN   = token ของบอท
  TELEGRAM_CHAT_ID = chat id ที่จะส่งข้อความไปให้

รันแบบ debug (ดู JSON ที่ดึงมาได้ เผื่อ TikTok เปลี่ยนโครงสร้างหน้า):
  python tiktok_live_notifier.py --debug username
"""

import os
import re
import sys
import json
import requests

STATE_FILE = "state.json"
USERNAMES_FILE = "usernames.txt"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_usernames():
    if not os.path.exists(USERNAMES_FILE):
        print(f"ไม่พบไฟล์ {USERNAMES_FILE}")
        return []
    with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lstrip("@") for line in f if line.strip() and not line.startswith("#")]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def extract_json_blob(html):
    """ดึง JSON ที่ TikTok ฝังไว้ในหน้าเว็บ (โครงสร้างนี้อาจเปลี่ยนได้ในอนาคต)"""
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def is_user_live(username, debug=False):
    """
    True  = กำลังไลฟ์
    False = ไม่ได้ไลฟ์
    None  = เช็คไม่ได้ (โครงสร้างหน้าเปลี่ยน / โดนบล็อก ฯลฯ)
    """
    url = f"https://www.tiktok.com/@{username}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"[{username}] เชื่อมต่อไม่ได้: {e}")
        return None

    if resp.status_code != 200:
        print(f"[{username}] status code {resp.status_code}")
        return None

    data = extract_json_blob(resp.text)
    if data is None:
        print(f"[{username}] หา JSON ในหน้าเว็บไม่เจอ (โครงสร้างอาจเปลี่ยน)")
        return None

    if debug:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:5000])

    try:
        user_detail = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]
    except (KeyError, TypeError):
        print(f"[{username}] ไม่พบข้อมูล user-detail ในหน้านี้ (อาจไม่มี user นี้จริง)")
        return None

    # roomId ที่ไม่ใช่ "0"/ไม่มีค่า มักหมายถึงกำลังไลฟ์อยู่
    room_id = None
    user_info = user_detail.get("userInfo", {})
    room_id = user_info.get("user", {}).get("roomId") or user_detail.get("roomId")

    return bool(room_id and str(room_id) != "0")


def send_telegram(message):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ไม่ได้ตั้งค่า TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=15)
        if r.status_code != 200:
            print(f"ส่ง Telegram ไม่สำเร็จ: {r.text}")
    except requests.RequestException as e:
        print(f"ส่ง Telegram ผิดพลาด: {e}")


def main():
    debug_user = None
    if "--debug" in sys.argv:
        idx = sys.argv.index("--debug")
        if idx + 1 < len(sys.argv):
            debug_user = sys.argv[idx + 1]

    if debug_user:
        is_user_live(debug_user, debug=True)
        return

    usernames = load_usernames()
    if not usernames:
        print("ไม่มี username ให้เช็ค (ดูไฟล์ usernames.txt)")
        return

    state = load_state()
    changed = False

    for username in usernames:
        live_now = is_user_live(username)
        if live_now is None:
            continue  # เช็คไม่ได้รอบนี้ ข้ามไปก่อน ไม่แก้ state

        was_live = state.get(username, False)

        if live_now and not was_live:
            send_telegram(f"🔴 @{username} เริ่มไลฟ์แล้ว!\nhttps://www.tiktok.com/@{username}/live")
            print(f"[{username}] แจ้งเตือน: เริ่มไลฟ์")
        elif not live_now and was_live:
            print(f"[{username}] ไลฟ์จบแล้ว")

        if live_now != was_live:
            state[username] = live_now
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
