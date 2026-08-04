"""
TikTok Live Notifier (v3)
--------------------------
ฟีเจอร์:
  - เช็คเฉพาะช่วงเวลา 08:00 - 01:00 (เวลาไทย) เท่านั้น
  - แจ้งเตือนตอนเริ่มไลฟ์ พร้อมระยะเวลาที่ไลฟ์มาแล้ว และจำนวนผู้ชม (ถ้าดึงได้)
  - มีปุ่ม "รับทราบแล้ว" ใต้ข้อความแจ้งเตือน
  - ถ้ายังไม่กดรับทราบ และยังไลฟ์อยู่ -> แจ้งเตือนซ้ำทุก ~5 นาที
  - พอกดรับทราบแล้ว จะหยุดแจ้งเตือนซ้ำ จนกว่าจะไลฟ์รอบใหม่ (ไลฟ์จบ -> เริ่มใหม่)
  - ส่งข้อความสถานะระบบวันละ 1 ครั้ง (heartbeat)
  - หน่วงเวลาสั้นๆ ระหว่างเช็คแต่ละช่อง กันโดน TikTok บล็อกจากการยิงถี่เกินไป

ต้องตั้ง environment variables:
  TELEGRAM_TOKEN   = token ของบอท
  TELEGRAM_CHAT_ID = chat id ที่จะส่งข้อความไปให้
"""

import os
import re
import sys
import json
import time
import random
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

STATE_FILE = "state.json"
USERNAMES_FILE = "usernames.txt"
TZ = ZoneInfo("Asia/Bangkok")
REMINDER_INTERVAL_SEC = 4.5 * 60  # กันเหนียวกรณี schedule คลาดเคลื่อน
DELAY_BETWEEN_CHECKS_SEC = (3, 6)  # หน่วงเวลาแบบสุ่มระหว่างเช็คแต่ละช่อง (วินาที)
MAX_RETRIES = 2

# ช่วงเวลาที่อนุญาตให้เช็ค: 08:00 ถึง 00:59 (เที่ยงคืนครึ่งหลัง จนถึงก่อนตี 1)
ACTIVE_HOUR_START = 8   # เริ่มเช็คตั้งแต่ชั่วโมงนี้
ACTIVE_HOUR_END_EXCLUSIVE = 1  # หยุดเช็คตอนถึงชั่วโมงนี้ (ตี 1)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

VIEWER_COUNT_KEYS = [
    "user_count", "userCount", "viewerCount", "viewer_count",
    "audienceCount", "audience_count", "total_user",
]


# ---------- เวลาทำงาน ----------

def is_within_active_hours(now_dt):
    hour = now_dt.hour
    # อนุญาต: 08:00-23:59 หรือ 00:00-00:59
    return hour >= ACTIVE_HOUR_START or hour < ACTIVE_HOUR_END_EXCLUSIVE


# ---------- ไฟล์ / state ----------

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
    return {"users": {}, "telegram_offset": 0, "last_daily_ping_date": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- ดึงข้อมูลจาก TikTok ----------

def extract_json_blob(html):
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html, re.S,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def find_first_key(obj, keys, _depth=0):
    if _depth > 12:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], (int, str)):
                try:
                    return int(obj[k])
                except (ValueError, TypeError):
                    pass
        for v in obj.values():
            found = find_first_key(v, keys, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = find_first_key(item, keys, _depth + 1)
            if found is not None:
                return found
    return None


def check_user(username, debug=False):
    """
    คืนค่า dict: {"live": bool/None, "viewer_count": int/None}
    live=None หมายถึงเช็คไม่ได้รอบนี้ (ไม่ควรเปลี่ยน state) - จะลองใหม่ MAX_RETRIES ครั้งก่อนยอมแพ้
    """
    url = f"https://www.tiktok.com/@{username}"
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
        except requests.RequestException as e:
            last_error = f"เชื่อมต่อไม่ได้: {e}"
            time.sleep(2)
            continue

        if resp.status_code != 200:
            last_error = f"status code {resp.status_code}"
            time.sleep(2)
            continue

        data = extract_json_blob(resp.text)
        if data is None:
            last_error = "หา JSON ในหน้าเว็บไม่เจอ (โครงสร้างอาจเปลี่ยน หรือโดนบล็อกชั่วคราว)"
            time.sleep(2)
            continue

        if debug:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:5000])

        try:
            user_detail = data["__DEFAULT_SCOPE__"]["webapp.user-detail"]
        except (KeyError, TypeError):
            print(f"[{username}] ไม่พบข้อมูล user-detail ในหน้านี้ (username อาจไม่ถูกต้อง)")
            return {"live": None, "viewer_count": None}

        user_info = user_detail.get("userInfo", {})
        room_id = user_info.get("user", {}).get("roomId") or user_detail.get("roomId")
        is_live = bool(room_id and str(room_id) != "0")

        viewer_count = None
        if is_live:
            viewer_count = find_first_key(user_detail, VIEWER_COUNT_KEYS)

        return {"live": is_live, "viewer_count": viewer_count}

    print(f"[{username}] เช็คไม่สำเร็จหลังลอง {MAX_RETRIES} ครั้ง ({last_error})")
    return {"live": None, "viewer_count": None}


# ---------- Telegram ----------

def tg_api(method, payload):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        print("ไม่ได้ตั้งค่า TELEGRAM_TOKEN")
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
        if not data.get("ok"):
            print(f"Telegram API error ({method}): {data}")
        return data
    except requests.RequestException as e:
        print(f"Telegram API ผิดพลาด ({method}): {e}")
        return None


def send_telegram(text, with_ack_button=False, username=None):
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        print("ไม่ได้ตั้งค่า TELEGRAM_CHAT_ID")
        return None
    payload = {"chat_id": chat_id, "text": text}
    if with_ack_button and username:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ รับทราบแล้ว", "callback_data": f"ack:{username}"}
            ]]
        }
    result = tg_api("sendMessage", payload)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def remove_ack_button(message_id, text):
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id or not message_id:
        return
    tg_api("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    })


def process_incoming_updates(state):
    """เช็คว่ามีคนกดปุ่ม 'รับทราบแล้ว' บ้างไหม แล้วอัปเดต state"""
    result = tg_api("getUpdates", {
        "offset": state.get("telegram_offset", 0),
        "timeout": 0,
        "allowed_updates": ["callback_query"],
    })
    if not result or not result.get("ok"):
        return

    for update in result.get("result", []):
        state["telegram_offset"] = update["update_id"] + 1
        cq = update.get("callback_query")
        if not cq:
            continue
        data = cq.get("data", "")
        if not data.startswith("ack:"):
            continue
        username = data.split(":", 1)[1]

        tg_api("answerCallbackQuery", {
            "callback_query_id": cq["id"],
            "text": "รับทราบแล้ว ขอบคุณครับ",
        })

        user_state = state["users"].get(username)
        if user_state and user_state.get("live"):
            user_state["acknowledged"] = True
            msg_id = user_state.get("message_id")
            if msg_id:
                remove_ack_button(
                    msg_id,
                    f"🔴 @{username} กำลังไลฟ์อยู่\n✅ รับทราบแล้ว"
                )


# ---------- Logic หลัก ----------

def format_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return "เพิ่งเริ่ม"
    if minutes < 60:
        return f"{minutes} นาที"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours} ชม. {rem} นาที"


def build_live_message(username, duration_text, viewer_count):
    viewer_text = f"{viewer_count} คน" if viewer_count is not None else "ไม่ทราบจำนวน"
    return (
        f"🔴 @{username} กำลังไลฟ์อยู่\n"
        f"⏱ ไลฟ์มาแล้ว: {duration_text}\n"
        f"👀 ผู้ชมตอนนี้: {viewer_text}\n"
        f"🔗 https://www.tiktok.com/@{username}/live\n\n"
        f"กดปุ่มด้านล่างถ้ารับทราบแล้ว ไม่งั้นจะแจ้งเตือนซ้ำทุก ~5 นาที"
    )


def handle_user(username, state, now_ts):
    check = check_user(username)
    if check["live"] is None:
        return  # เช็คไม่ได้รอบนี้ ข้าม ไม่แตะ state

    users = state["users"]
    prev = users.get(username, {})
    was_live = prev.get("live", False)

    if check["live"] and not was_live:
        # เพิ่งเริ่มไลฟ์รอบใหม่ -> รีเซ็ต acknowledged เป็น False เสมอ
        start_time = now_ts
        duration_text = format_duration(0)
        text = build_live_message(username, duration_text, check["viewer_count"])
        msg_id = send_telegram(text, with_ack_button=True, username=username)
        users[username] = {
            "live": True,
            "start_time": start_time,
            "acknowledged": False,
            "last_reminder": now_ts,
            "message_id": msg_id,
        }
        print(f"[{username}] แจ้งเตือน: เริ่มไลฟ์")

    elif check["live"] and was_live:
        # ยังไลฟ์อยู่ต่อเนื่อง
        start_time = prev.get("start_time", now_ts)
        acknowledged = prev.get("acknowledged", False)
        last_reminder = prev.get("last_reminder", start_time)

        if not acknowledged and (now_ts - last_reminder) >= REMINDER_INTERVAL_SEC:
            duration_text = format_duration(now_ts - start_time)
            text = build_live_message(username, duration_text, check["viewer_count"])
            msg_id = send_telegram(text, with_ack_button=True, username=username)
            prev["last_reminder"] = now_ts
            prev["message_id"] = msg_id
            print(f"[{username}] แจ้งเตือนซ้ำ (ยังไม่ได้รับทราบ)")
        users[username] = prev

    elif not check["live"] and was_live:
        # ไลฟ์จบแล้ว -> เคลียร์สถานะทั้งหมด รอบหน้าไลฟ์ใหม่จะแจ้งเตือนใหม่เสมอ
        print(f"[{username}] ไลฟ์จบแล้ว")
        users[username] = {"live": False}

    else:
        users.setdefault(username, {"live": False})


def send_daily_ping(state, usernames, now_dt):
    today_str = now_dt.strftime("%Y-%m-%d")
    if state.get("last_daily_ping_date") == today_str:
        return
    time_str = now_dt.strftime("%H:%M")
    text = (
        f"✅ ระบบแจ้งเตือน TikTok Live ทำงานปกติ\n"
        f"📅 {today_str} เวลา {time_str} น.\n"
        f"👁 กำลังติดตาม {len(usernames)} ช่อง: "
        + ", ".join(f"@{u}" for u in usernames)
    )
    send_telegram(text)
    state["last_daily_ping_date"] = today_str
    print("ส่งข้อความสถานะระบบประจำวันแล้ว")


def main():
    debug_user = None
    if "--debug" in sys.argv:
        idx = sys.argv.index("--debug")
        if idx + 1 < len(sys.argv):
            debug_user = sys.argv[idx + 1]

    if debug_user:
        check_user(debug_user, debug=True)
        return

    now_dt = datetime.now(TZ)

    if not is_within_active_hours(now_dt):
        print(f"อยู่นอกช่วงเวลาทำงาน ({now_dt.strftime('%H:%M')} น.) ข้ามรอบนี้ (ทำงาน 08:00-01:00)")
        return

    usernames = load_usernames()
    if not usernames:
        print("ไม่มี username ให้เช็ค (ดูไฟล์ usernames.txt)")
        return

    state = load_state()
    now_ts = time.time()

    process_incoming_updates(state)

    for i, username in enumerate(usernames):
        try:
            handle_user(username, state, now_ts)
        except Exception as e:
            print(f"[{username}] เกิดข้อผิดพลาดไม่คาดคิด: {type(e).__name__}: {e}")
        if i < len(usernames) - 1:
            time.sleep(random.uniform(*DELAY_BETWEEN_CHECKS_SEC))

    send_daily_ping(state, usernames, now_dt)

    save_state(state)


if __name__ == "__main__":
    main()
