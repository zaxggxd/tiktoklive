"""
TikTok Live Notifier (v4 - Simplified)
----------------------------------------
ฟีเจอร์:
  - เช็คเฉพาะช่วงเวลา 08:00 - 01:00 (เวลาไทย) เท่านั้น
  - แจ้งเตือนตอนเริ่มไลฟ์ (ข้อความเดียว ไม่มีปุ่ม ไม่เตือนซ้ำ)
  - แจ้งเตือนตอนไลฟ์จบ พร้อมระยะเวลารวมที่ไลฟ์ไป
  - ส่งข้อความสถานะระบบวันละ 1 ครั้ง (heartbeat)
  - หน่วงเวลาสั้นๆ ระหว่างเช็คแต่ละช่อง กันโดน TikTok บล็อกจากการยิงถี่เกินไป
  - แต่ละช่องเช็คแยกกัน ถ้าช่องไหน error จะไม่กระทบช่องอื่น

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
DELAY_BETWEEN_CHECKS_SEC = (3, 6)  # หน่วงเวลาแบบสุ่มระหว่างเช็คแต่ละช่อง (วินาที)
MAX_RETRIES = 2

# ช่วงเวลาที่อนุญาตให้เช็ค: 08:00 ถึง 00:59 (เที่ยงคืนครึ่งหลัง จนถึงก่อนตี 1)
ACTIVE_HOUR_START = 11
ACTIVE_HOUR_END_EXCLUSIVE = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- เวลาทำงาน ----------

def is_within_active_hours(now_dt):
    hour = now_dt.hour
    return hour >= ACTIVE_HOUR_START or hour < ACTIVE_HOUR_END_EXCLUSIVE


# ---------- ไฟล์ / state ----------

def load_usernames():
    # ลำดับความสำคัญ: อ่านจาก Secret (TIKTOK_USERNAMES) ก่อน เพื่อไม่ให้ชื่อช่องเปิดเผยแบบ public
    env_value = os.environ.get("TIKTOK_USERNAMES", "").strip()
    if env_value:
        # รองรับทั้งคั่นด้วย comma และขึ้นบรรทัดใหม่
        raw_items = re.split(r"[,\n]+", env_value)
        return [u.strip().lstrip("@") for u in raw_items if u.strip()]

    if not os.path.exists(USERNAMES_FILE):
        print(f"ไม่พบไฟล์ {USERNAMES_FILE} และไม่ได้ตั้งค่า Secret TIKTOK_USERNAMES")
        return []
    with open(USERNAMES_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lstrip("@") for line in f if line.strip() and not line.startswith("#")]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "last_daily_ping_date": ""}


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


def check_user_live(username, debug=False):
    """
    คืนค่า True/False/None
    None หมายถึงเช็คไม่ได้รอบนี้ (ไม่ควรเปลี่ยน state) - ลองใหม่ MAX_RETRIES ครั้งก่อนยอมแพ้
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
            return None

        user_info = user_detail.get("userInfo", {})
        room_id = user_info.get("user", {}).get("roomId") or user_detail.get("roomId")
        return bool(room_id and str(room_id) != "0")

    print(f"[{username}] เช็คไม่สำเร็จหลังลอง {MAX_RETRIES} ครั้ง ({last_error})")
    return None


# ---------- Telegram ----------

def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ไม่ได้ตั้งค่า TELEGRAM_TOKEN / TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if r.status_code != 200:
            print(f"ส่ง Telegram ไม่สำเร็จ: {r.text}")
    except requests.RequestException as e:
        print(f"ส่ง Telegram ผิดพลาด: {e}")


# ---------- Logic หลัก ----------

def format_duration(seconds):
    minutes = int(seconds // 60)
    if minutes < 1:
        return "ไม่ถึง 1 นาที"
    if minutes < 60:
        return f"{minutes} นาที"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours} ชม. {rem} นาที"


def handle_user(username, state, now_ts):
    live_now = check_user_live(username)
    if live_now is None:
        return  # เช็คไม่ได้รอบนี้ ข้าม ไม่แตะ state

    users = state["users"]
    prev = users.get(username, {})
    was_live = prev.get("live", False)

    if live_now and not was_live:
        # เพิ่งเริ่มไลฟ์
        send_telegram(
            f"🔴 @{username} เริ่มไลฟ์แล้ว!\n"
            f"🔗 https://www.tiktok.com/@{username}/live"
        )
        users[username] = {"live": True, "start_time": now_ts}
        print(f"[{username}] แจ้งเตือน: เริ่มไลฟ์")

    elif not live_now and was_live:
        # ไลฟ์จบแล้ว
        start_time = prev.get("start_time", now_ts)
        duration_text = format_duration(now_ts - start_time)
        send_telegram(
            f"⚫ @{username} ไลฟ์จบแล้ว\n"
            f"⏱ ไลฟ์ไปทั้งหมด: {duration_text}"
        )
        users[username] = {"live": False}
        print(f"[{username}] แจ้งเตือน: ไลฟ์จบแล้ว ({duration_text})")

    else:
        # สถานะไม่เปลี่ยน ไม่ต้องทำอะไร
        users.setdefault(username, {"live": live_now})


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
        check_user_live(debug_user, debug=True)
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
