import json
import os
import time
import re
import requests
from bs4 import BeautifulSoup

from config import BOT_TOKEN, source_channels, target_channel


SIGNATURE = "⚽ فوتبال برتر\n@footbalbartar99"

STATE_FILE = "monitor_state.json"

CHECK_INTERVAL = 30
RETRIES = 3


class TelegramSendUncertain(Exception):
    """Telegram may have received the message, but the response was lost."""
    pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0 Mobile Safari/537.36"
    )
}


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


def get_channel_html(channel):
    url = f"https://t.me/s/{channel}"

    for attempt in range(1, RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers=HEADERS,
                timeout=30
            )

            if response.status_code == 200:
                return response.text

            print(
                f"[{channel}] HTTP {response.status_code}"
            )

        except requests.RequestException as e:
            print(
                f"[{channel}] connection error "
                f"(attempt {attempt}/{RETRIES}): {e}"
            )

        if attempt < RETRIES:
            time.sleep(2)

    return None


def get_posts(channel):
    html = get_channel_html(channel)

    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    return soup.select(".tgme_widget_message")


def get_post_id(post):
    return post.get("data-post")


def clean_text(text):
    if not text:
        return ""

    # حذف آدرس و لینک کانال‌های منبع
    for channel in source_channels:
        pattern = rf"(?i)(?<![A-Za-z0-9_])@{re.escape(channel)}\b"
        text = re.sub(pattern, "", text)

        pattern = rf"(?i)https?://t\.me/{re.escape(channel)}(?:/\d+)?"
        text = re.sub(pattern, "", text)

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]
    cleaned = []

    for line in lines:
        # حذف امضای مستقل کانال، مثل @TajFans1945
        if re.fullmatch(r"@[A-Za-z0-9_]{3,}", line.strip()):
            continue

        cleaned.append(line)
    text = "\n".join(cleaned).strip()

    # حذف فاصله‌های اضافی باقی‌مانده
    text = re.sub(r"\n{3,}", "\n\n", text)

    if text:
        return f"{text}\n\n{SIGNATURE}"

    return SIGNATURE

def get_post_text(post):
    text_node = post.select_one(
        ".tgme_widget_message_text"
    )

    if not text_node:
        return ""

    return text_node.get_text(
        "\n",
        strip=True
    )


def get_video_url(post):
    videos = post.select("video")

    if not videos:
        return None

    video = videos[0]

    video_url = video.get("src")

    if not video_url:
        source = video.select_one("source")

        if source:
            video_url = source.get("src")

    return video_url


def get_photo_url(post):
    photo = post.select_one(
        ".tgme_widget_message_photo_wrap"
    )

    if not photo:
        return None

    style = photo.get("style", "")

    marker = "url('"

    if marker in style:
        start = style.find(marker) + len(marker)
        end = style.find("'", start)

        if end != -1:
            return style[start:end]

    marker = 'url("'

    if marker in style:
        start = style.find(marker) + len(marker)
        end = style.find('"', start)

        if end != -1:
            return style[start:end]

    return None


def detect_post(post):
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
        "id": get_post_id(post),
        "type": content_type,
        "text": text,
        "video_url": video_url,
        "photo_url": photo_url,
    }


def telegram_url(method):
    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def send_text(text):
    try:
        response = requests.post(
            telegram_url("sendMessage"),
            data={
                "chat_id": f"@{target_channel}",
                "text": text,
            },
            timeout=60
        )
    except requests.RequestException as e:
        raise TelegramSendUncertain(
            f"sendMessage network error: {e}"
        ) from e

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as e:
        raise TelegramSendUncertain(
            "sendMessage returned invalid JSON"
        ) from e

    if not data.get("ok"):
        raise Exception(data)

    return data


def send_video(video_url, caption):
    print("Downloading video...")

    with requests.get(
        video_url,
        headers=HEADERS,
        stream=True,
        timeout=120
    ) as video_response:

        video_response.raise_for_status()

        print(
            "VIDEO HTTP:",
            video_response.status_code
        )

        print(
            "CONTENT-TYPE:",
            video_response.headers.get(
                "content-type"
            )
        )

        print(
            "CONTENT-LENGTH:",
            video_response.headers.get(
                "content-length"
            )
        )

        try:
            response = requests.post(
                telegram_url("sendVideo"),
                data={
                    "chat_id": f"@{target_channel}",
                    "caption": caption[:1024],
                },
                files={
                    "video": (
                        "video.mp4",
                        video_response.raw,
                        "video/mp4"
                    )
                },
                timeout=180
            )
        except requests.RequestException as e:
            raise TelegramSendUncertain(
                f"sendVideo network error: {e}"
            ) from e

    response.raise_for_status()

    try:
        data = response.json()
    except ValueError as e:
        raise TelegramSendUncertain(
            "sendVideo returned invalid JSON"
        ) from e

    if not data.get("ok"):
        raise Exception(data)

    return data


def send_photo(photo_url, caption):
    print("Downloading photo...")

    response = requests.get(
        photo_url,
        headers=HEADERS,
        timeout=60
    )

    response.raise_for_status()

    try:
        telegram_response = requests.post(
            telegram_url("sendPhoto"),
            data={
                "chat_id": f"@{target_channel}",
                "caption": caption[:1024],
            },
            files={
                "photo": (
                    "photo.jpg",
                    response.content,
                    "image/jpeg"
                )
            },
            timeout=120
        )
    except requests.RequestException as e:
        raise TelegramSendUncertain(
            f"sendPhoto network error: {e}"
        ) from e

    telegram_response.raise_for_status()

    try:
        data = telegram_response.json()
    except ValueError as e:
        raise TelegramSendUncertain(
            "sendPhoto returned invalid JSON"
        ) from e

    if not data.get("ok"):
        raise Exception(data)

    return data


def normalize_for_match(text):
    if not text:
        return ""

    # حذف امضای بات فقط برای مقایسه
    text = re.sub(
        r"⚽\s*فوتبال برتر\s*@footbalbartar99",
        "",
        text
    )

    # حذف تفاوت فاصله و شکست خطوط تلگرام
    text = re.sub(r"\s+", "", text)

    return text.strip()


def destination_has_post(post):
    try:
        response = requests.get(
            f"https://t.me/s/{target_channel}",
            headers=HEADERS,
            timeout=30
        )

        if response.status_code != 200:
            return False

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        wanted_text = normalize_for_match(
            post.get("text", "")
        )

        wanted_type = post.get("type")

        for item in soup.select(".tgme_widget_message"):
            item_type = (
                "video"
                if item.select("video")
                else "photo"
                if item.select_one(
                    ".tgme_widget_message_photo_wrap"
                )
                else "text"
            )

            if item_type != wanted_type:
                continue

            node = item.select_one(
                ".tgme_widget_message_text"
            )

            item_text = (
                node.get_text(
                    "\n",
                    strip=True
                )
                if node
                else ""
            )

            if normalize_for_match(item_text) == wanted_text:
                print(
                    "CONFIRMED ON DESTINATION:",
                    item.get("data-post")
                )
                return True

        return False

    except requests.RequestException as e:
        print(
            "Destination check failed:",
            str(e)[:300]
        )
        return False


def confirm_destination(post, attempts=3, delay=4):
    for attempt in range(1, attempts + 1):
        print(
            f"Checking destination "
            f"(attempt {attempt}/{attempts})..."
        )

        if destination_has_post(post):
            return True

        if attempt < attempts:
            time.sleep(delay)

    return False


def publish_post(post):
    post_type = post["type"]

    caption = clean_text(
        post["text"]
    )

    print(
        "Publishing:",
        post["id"],
        "| TYPE:",
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

    message = result.get("result", {})

    print(
        "PUBLISHED:",
        message.get("message_id")
    )


def initialize_state():
    state = {}

    print("\nInitializing monitor...")
    print("Old posts will NOT be published.\n")

    for channel in source_channels:
        posts = get_posts(channel)

        if not posts:
            print(
                f"[{channel}] no posts found"
            )
            continue

        latest = posts[-1]
        latest_id = get_post_id(latest)

        state[channel] = latest_id

        print(
            f"[{channel}] latest = {latest_id}"
        )

    save_state(state)

    print("\nInitialization complete.")
    print("Waiting for NEW posts...\n")

    return state


def get_numeric_post_id(post_id):
    try:
        return int(post_id.rsplit("/", 1)[-1])
    except (ValueError, AttributeError):
        return None


def monitor():
    state = load_state()

    if not state:
        state = initialize_state()

    while True:
        for channel in source_channels:
            try:
                posts = get_posts(channel)

                if not posts:
                    continue

                known_id = state.get(channel)

                if known_id is None:
                    continue

                known_num = get_numeric_post_id(known_id)

                if known_num is None:
                    print(f"[{channel}] Invalid state ID:", known_id)
                    continue

                new_posts = []

                for post in posts:
                    post_id = get_post_id(post)

                    if not post_id:
                        continue

                    post_num = get_numeric_post_id(post_id)

                    if post_num is not None and post_num > known_num:
                        new_posts.append((post_num, post))

                if not new_posts:
                    continue

                new_posts.sort(key=lambda item: item[0])

                print(f"\n[{channel}] NEW POSTS: {len(new_posts)}")

                for post_num, post in new_posts:
                    post_data = detect_post(post)

                    print("NEW:", post_data["id"], "|", post_data["type"])

                    state_confirmed = False

                    try:
                        publish_post(post_data)
                        state_confirmed = True

                    except TelegramSendUncertain as e:
                        print("UNCERTAIN SEND:", str(e)[:300])
                        print("Verifying destination...")

                        confirmed = confirm_destination(
                            post_data,
                            attempts=3,
                            delay=4
                        )

                        if confirmed:
                            print("SEND CONFIRMED ON DESTINATION.")
                            state_confirmed = True

                        else:
                            print("NOT FOUND ON DESTINATION.")
                            print("Retrying publish once...")

                            try:
                                publish_post(post_data)

                                print("RETRY SEND RETURNED SUCCESS.")
                                print("Verifying retry on destination...")

                                retry_confirmed = confirm_destination(
                                    post_data,
                                    attempts=3,
                                    delay=4
                                )

                                if retry_confirmed:
                                    print("RETRY SEND CONFIRMED ON DESTINATION.")
                                    state_confirmed = True
                                else:
                                    print("RETRY NOT CONFIRMED.")
                                    print("STATE WILL NOT BE UPDATED.")

                            except TelegramSendUncertain as retry_error:
                                print(
                                    "RETRY ALSO UNCERTAIN:",
                                    str(retry_error)[:300]
                                )
                                print("STATE WILL NOT BE UPDATED.")

                            except Exception as retry_error:
                                print(
                                    "RETRY FAILED:",
                                    str(retry_error)[:500]
                                )
                                print("STATE WILL NOT BE UPDATED.")

                    if state_confirmed:
                        state[channel] = post_data["id"]
                        save_state(state)
                        print("STATE UPDATED:", post_data["id"])
                    else:
                        print(
                            "STATE NOT UPDATED:",
                            post_data["id"]
                        )

            except Exception as e:
                print(f"[{channel}] ERROR:", str(e)[:500])

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    print("=" * 60)
    print("FOOTBALL BARTAR AUTO MONITOR")
    print("=" * 60)
    print("Channels:", ", ".join(source_channels))
    print("Target:", target_channel)
    print("Check interval:", CHECK_INTERVAL, "seconds")
    print("=" * 60)

    monitor()
