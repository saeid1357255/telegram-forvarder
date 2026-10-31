import json
import os
import re
import time
import tempfile
import requests

from bs4 import BeautifulSoup
from config import BOT_TOKEN, source_channels, target_channel


# =========================
# SETTINGS
# =========================

SIGNATURE = "⚽ فوتبال برتر\n@footbalbartar99"

STATE_FILE = "monitor_state.json"

CHECK_INTERVAL = 30
RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Mobile Safari/537.36"
    )
}


# =========================
# EXCEPTIONS
# =========================

class TelegramSendUncertain(Exception):
    pass


class TelegramRejected(Exception):
    pass


class SourceUnavailable(Exception):
    pass


# =========================
# STATE MANAGEMENT
# =========================

def load_state():
    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        raise RuntimeError(
            f"STATE FILE EXISTS BUT CANNOT BE READ: {e}"
        ) from e

    if not isinstance(state, dict) or not state:
        raise RuntimeError(
            "STATE FILE IS INVALID OR EMPTY. "
            "AUTOMATIC INITIALIZATION BLOCKED."
        )

    pending = state.get("_pending", {})
    if not isinstance(pending, dict):
        raise RuntimeError("INVALID PENDING STATE")

    return state


def save_state(state):

    directory = os.path.dirname(
        os.path.abspath(STATE_FILE)
    )

    fd, temp_path = tempfile.mkstemp(
        dir=directory,
        prefix="monitor_state_",
        suffix=".tmp"
    )

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(
            temp_path,
            STATE_FILE
        )

    except Exception:

        if os.path.exists(temp_path):
            os.remove(temp_path)

        raise


# =========================
# HTTP HELPERS
# =========================

def get_channel_html(channel):

    url = f"https://t.me/s/{channel}"

    for attempt in range(1, RETRIES + 1):

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30
            )

            response.raise_for_status()

            return response.text

        except requests.RequestException as e:

            print(
                f"[{channel}] SOURCE ERROR "
                f"{attempt}/{RETRIES}: {e}"
            )

            if attempt < RETRIES:
                time.sleep(3)

    return None


def get_posts(channel):

    html = get_channel_html(channel)

    if html is None:
        raise SourceUnavailable(channel)

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    return soup.select(
        ".tgme_widget_message[data-post]"
    )


# =========================
# POST EXTRACTION
# =========================

def get_post_id(post):

    return post.get("data-post")


def get_numeric_post_id(post_id):

    try:

        return int(
            post_id.rsplit("/", 1)[-1]
        )

    except (
        ValueError,
        AttributeError
    ):

        return None


def get_post_text(post):

    node = post.select_one(
        ".tgme_widget_message_text"
    )

    if not node:
        return ""

    return node.get_text(
        "\n",
        strip=True
    )


def extract_background_url(style):

    match = re.search(
        r"""url\(['"]?(.*?)['"]?\)""",
        style
    )

    if match:
        return match.group(1)

    return None


def get_photo_url(post):

    photo = post.select_one(
        ".tgme_widget_message_photo_wrap"
    )

    if not photo:
        return None

    return extract_background_url(
        photo.get("style", "")
    )


def get_video_url(post):

    video = post.select_one("video")

    if not video:
        return None

    url = video.get("src")

    if url:
        return url

    source = video.select_one("source")

    if source:
        return source.get("src")

    return None


def detect_post(post):

    post_id = get_post_id(post)

    text = get_post_text(post)

    video_url = get_video_url(post)

    photo_url = get_photo_url(post)

    if video_url:

        content_type = "video"

    elif photo_url:

        content_type = "photo"

    else:

        content_type = "text"

    return {
        "id": post_id,
        "type": content_type,
        "text": text,
        "video_url": video_url,
        "photo_url": photo_url
    }


# =========================
# TEXT CLEANING
# =========================

def clean_text(text):

    if not text:
        return SIGNATURE

    for channel in source_channels:

        text = re.sub(
            rf"(?i)(?<![A-Za-z0-9_])@{re.escape(channel)}\b",
            "",
            text
        )

        text = re.sub(
            rf"(?i)https?://t\.me/{re.escape(channel)}(?:/\d+)?",
            "",
            text
        )

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    cleaned = []

    for line in lines:

        if re.fullmatch(
            r"@[A-Za-z0-9_]{3,}",
            line
        ):
            continue

        cleaned.append(line)

    text = "\n".join(cleaned).strip()

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    if text:
        return f"{text}\n\n{SIGNATURE}"

    return SIGNATURE


# =========================
# TELEGRAM API
# =========================

def telegram_url(method):

    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def check_telegram_response(response):
    try:
        data = response.json()
    except (ValueError, TypeError) as e:
        raise TelegramSendUncertain(
            "Telegram returned invalid JSON"
        ) from e

    if not isinstance(data, dict):
        raise TelegramSendUncertain(
            "Telegram returned an invalid response structure"
        )

    if response.status_code >= 500:
        raise TelegramSendUncertain(
            f"Telegram server error HTTP {response.status_code}: {data}"
        )

    if data.get("ok") is False:
        raise TelegramRejected(
            f"Telegram explicitly rejected request: {data}"
        )

    if data.get("ok") is not True:
        raise TelegramSendUncertain(
            f"Telegram response has no confirmed success status: {data}"
        )

    try:
        response.raise_for_status()
    except requests.RequestException as e:
        raise TelegramSendUncertain(
            f"HTTP error despite success response: {e}"
        ) from e

    return data



def send_text(text):

    try:

        response = requests.post(
            telegram_url("sendMessage"),
            data={
                "chat_id": f"@{target_channel}",
                "text": text
            },
            timeout=60
        )

    except requests.RequestException as e:

        raise TelegramSendUncertain(
            f"sendMessage network error: {e}"
        ) from e

    return check_telegram_response(response)


def send_video(video_url, caption):

    print("Downloading video...")

    with requests.get(
        video_url,
        headers=HEADERS,
        stream=True,
        timeout=(30, 120)
    ) as video_response:

        video_response.raise_for_status()

        try:

            response = requests.post(
                telegram_url("sendVideo"),
                data={
                    "chat_id": f"@{target_channel}",
                    "caption": caption[:1024]
                },
                files={
                    "video": (
                        "video.mp4",
                        video_response.raw,
                        "video/mp4"
                    )
                },
                timeout=(30, 180)
            )

        except requests.RequestException as e:

            raise TelegramSendUncertain(
                f"sendVideo network error: {e}"
            ) from e

    return check_telegram_response(response)


def send_photo(photo_url, caption):

    print("Downloading photo...")

    photo_response = requests.get(
        photo_url,
        headers=HEADERS,
        timeout=60
    )

    photo_response.raise_for_status()

    try:

        response = requests.post(
            telegram_url("sendPhoto"),
            data={
                "chat_id": f"@{target_channel}",
                "caption": caption[:1024]
            },
            files={
                "photo": (
                    "photo.jpg",
                    photo_response.content,
                    "image/jpeg"
                )
            },
            timeout=120
        )

    except requests.RequestException as e:

        raise TelegramSendUncertain(
            f"sendPhoto network error: {e}"
        ) from e

    return check_telegram_response(response)


# =========================
# DESTINATION VERIFICATION
# =========================

def normalize_for_match(text):

    if not text:
        return ""

    text = re.sub(
        r"⚽\s*فوتبال برتر\s*@footbalbartar99",
        "",
        text
    )

    return re.sub(
        r"\s+",
        "",
        text
    ).strip()


def destination_has_post(post):
    response = requests.get(
        f"https://t.me/s/{target_channel}",
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    wanted_text = normalize_for_match(post.get("text", ""))

    if not wanted_text:
        return "UNKNOWN"

    wanted_type = post.get("type")

    for item in soup.select(".tgme_widget_message"):
        if item.select_one("video"):
            item_type = "video"
        elif item.select_one(".tgme_widget_message_photo_wrap"):
            item_type = "photo"
        else:
            item_type = "text"

        if item_type != wanted_type:
            continue

        node = item.select_one(".tgme_widget_message_text")
        item_text = (
            node.get_text("\\n", strip=True)
            if node else ""
        )

        if normalize_for_match(item_text) == wanted_text:
            print(
                "CONFIRMED ON DESTINATION:",
                item.get("data-post")
            )
            return "FOUND"

    return "NOT_FOUND"


def confirm_destination(post, attempts=4, delay=5):
    uncertain = False

    for attempt in range(1, attempts + 1):
        print(f"DESTINATION CHECK {attempt}/{attempts}")

        try:
            result = destination_has_post(post)

            if result == "FOUND":
                return "FOUND"

            if result != "NOT_FOUND":
                uncertain = True

        except requests.RequestException as e:
            uncertain = True
            print("DESTINATION CHECK ERROR:", str(e)[:300])

        if attempt < attempts:
            time.sleep(delay)

    if uncertain:
        return "UNKNOWN"

    return "NOT_FOUND"


def publish_post(post):

    caption = clean_text(
        post["text"]
    )

    post_type = post["type"]

    print(
        "PUBLISHING:",
        post["id"],
        "|",
        post_type
    )

    if post_type == "video":

        result = send_video(
            post["video_url"],
            caption
        )

    elif post_type == "photo":

        result = send_photo(
            post["photo_url"],
            caption
        )

    else:

        result = send_text(
            caption
        )

    message = result.get(
        "result",
        {}
    )

    message_id = message.get("message_id")
    if not message_id:
        raise TelegramSendUncertain(
            "Telegram response did not contain message_id"
        )

    print("PUBLISHED:", message_id)
    return {
        "message_id": message_id,
        "chat": message.get("chat", {})
    }


# =========================
# INITIALIZATION
# =========================

def initialize_state():

    state = {}

    print("\nINITIALIZING MONITOR")

    print(
        "Old posts will NOT be published."
    )

    for channel in source_channels:

        try:

            posts = get_posts(channel)

        except SourceUnavailable:

            print(
                f"[{channel}] INITIALIZATION FAILED"
            )

            raise

        if not posts:

            print(
                f"[{channel}] No posts found"
            )

            raise SourceUnavailable(channel)

        latest_id = get_post_id(
            posts[-1]
        )

        if not latest_id:
            raise SourceUnavailable(channel)

        state[channel] = latest_id

        print(
            f"[{channel}] LATEST: {latest_id}"
        )

    save_state(state)

    print("INITIALIZATION COMPLETE")

    return state


# =========================
# CHANNEL PROCESSING
# =========================

def process_channel(channel, state):
    pending_all = state.setdefault("_pending", {})

    if not isinstance(pending_all, dict):
        raise RuntimeError("INVALID PENDING STATE")

    pending_record = pending_all.get(channel)

    if pending_record is not None:
        if (
            not isinstance(pending_record, dict)
            or not pending_record.get("id")
            or not isinstance(pending_record.get("post"), dict)
        ):
            print(f"[{channel}] INVALID PENDING RECORD. STOPPING.")
            return

        pending_id = pending_record["id"]
        print(f"[{channel}] RECONCILING PENDING POST:", pending_id)

        try:
            result = confirm_destination(pending_record["post"])
        except Exception as e:
            print("PENDING RECONCILIATION ERROR:", str(e)[:300])
            print("PENDING PRESERVED. NO RESEND.")
            return

        if result != "FOUND":
            print("PENDING RESULT:", result)
            print("PENDING PRESERVED. AUTOMATIC RESEND BLOCKED.")
            return

        state[channel] = pending_id
        pending_all.pop(channel)
        save_state(state)

        print("PENDING POST CONFIRMED AND STATE UPDATED:", pending_id)

    posts = get_posts(channel)

    if not posts:
        print(f"[{channel}] No posts found.")
        return

    known_id = state.get(channel)

    if known_id is None:
        print(f"[{channel}] State missing. Skipping channel.")
        return

    known_num = get_numeric_post_id(known_id)

    if known_num is None:
        print(f"[{channel}] INVALID STATE:", known_id)
        return

    new_posts = []

    for post in posts:
        post_id = get_post_id(post)

        if not post_id:
            continue

        post_num = get_numeric_post_id(post_id)

        if post_num is not None and post_num > known_num:
            new_posts.append((post_num, post))

    if not new_posts:
        print(f"[{channel}] No new posts.")
        return

    new_posts.sort(key=lambda item: item[0])

    print(f"\n[{channel}] NEW POSTS: {len(new_posts)}")

    for _, post in new_posts:
        post_data = detect_post(post)
        post_id = post_data.get("id")

        print("\nNEW POST:", post_id)

        if not post_id:
            print("INVALID POST ID. STOPPING CHANNEL.")
            break

        pending_all[channel] = {
            "id": post_id,
            "post": post_data
        }

        try:
            save_state(state)
        except Exception as e:
            pending_all.pop(channel, None)
            print("PENDING SAVE FAILED:", str(e)[:300])
            print("SEND BLOCKED. NO MESSAGE WAS SENT.")
            break

        print("PENDING SAVED BEFORE SEND:", post_id)

        try:
            result = publish_post(post_data)

            if not isinstance(result, dict) or not result.get("message_id"):
                raise TelegramSendUncertain(
                    "Publish result has no confirmed message_id"
                )

            state[channel] = post_id
            pending_all.pop(channel, None)
            save_state(state)

            print("STATE UPDATED:", post_id)
            print("TELEGRAM MESSAGE ID:", result["message_id"])

        except TelegramRejected as e:
            print("TELEGRAM EXPLICITLY REJECTED POST:", str(e)[:500])

            rejected_pending = pending_all.pop(channel, None)

            try:
                save_state(state)
            except Exception as save_error:
                if rejected_pending is not None:
                    pending_all[channel] = rejected_pending

                print("PENDING CLEAR SAVE FAILED:", str(save_error)[:300])
                print("PENDING RESTORED. CHANNEL STOPPED.")
                break

            print("REJECTED POST PENDING CLEARED.")
            print("SOURCE STATE UNCHANGED. RETRY DEFERRED TO NEXT CYCLE.")
            break

        except TelegramSendUncertain as e:
            print("UNCERTAIN SEND:", str(e)[:300])
            print("Checking destination before resolving pending...")

            try:
                destination_result = confirm_destination(post_data)
            except Exception as check_error:
                destination_result = "UNKNOWN"
                print("DESTINATION CHECK ERROR:", str(check_error)[:300])

            if destination_result == "FOUND":
                state[channel] = post_id
                pending_all.pop(channel, None)
                save_state(state)
                print("POST CONFIRMED. STATE UPDATED:", post_id)
            else:
                print("DESTINATION RESULT:", destination_result)
                print("PENDING PRESERVED. AUTOMATIC RESEND BLOCKED.")

                try:
                    save_state(state)
                except Exception as save_error:
                    print("WARNING: PENDING SAVE ERROR:", str(save_error)[:300])

                break

        except Exception as e:
            print(f"[{channel}] PUBLISH ERROR:", str(e)[:500])
            print("PENDING PRESERVED. AUTOMATIC RESEND BLOCKED.")

            try:
                save_state(state)
            except Exception as save_error:
                print("WARNING: PENDING SAVE ERROR:", str(save_error)[:300])

            break



# =========================
# MONITORING CYCLE
# =========================

def run_one_cycle(state):

    print("\n========== NEW CYCLE ==========")

    for channel in source_channels:

        try:

            process_channel(
                channel,
                state
            )

        except SourceUnavailable:

            print(
                f"[{channel}] SOURCE UNAVAILABLE"
            )

        except Exception as e:

            print(
                f"[{channel}] CYCLE ERROR:",
                str(e)[:500]
            )

    print("========== CYCLE COMPLETE ==========")


# =========================
# MAIN MONITOR
# =========================

def monitor():
    try:
        state = load_state()
    except Exception as e:
        print("FATAL STATE ERROR:", str(e))
        print("MONITOR STOPPED. STATE WAS NOT REINITIALIZED.")
        return

    if state is None:
        try:
            state = initialize_state()
        except Exception as e:
            print("INITIALIZATION ERROR:", str(e))
            return

    run_one_cycle(state)

    if os.getenv("GITHUB_ACTIONS") == "true":
        print("\\nGITHUB ACTIONS: ONE-CYCLE RUN COMPLETED.")
        return

    print("\\nTERMUX CONTINUOUS MONITOR STARTED.")
    while True:
        time.sleep(CHECK_INTERVAL)
        run_one_cycle(state)


# =========================
# ENTRY POINT
# =========================

if __name__ == "__main__":

    print("=" * 55)

    print(
        "FOOTBALL BARTAR AUTO MONITOR"
    )

    print("=" * 55)

    print(
        "SOURCE CHANNELS:",
        ", ".join(source_channels)
    )

    print(
        "TARGET CHANNEL:",
        target_channel
    )

    print(
        "CHECK INTERVAL:",
        CHECK_INTERVAL,
        "SECONDS"
    )

    print("=" * 55)

    monitor()
