"""Cloudflare Worker (Python) — Telegram-бот QR.

Работает через webhook: Telegram сам присылает сообщение на URL воркера.
"""

import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import qrcode
import requests
from PIL import Image
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

URL_RE = re.compile(r"^(https?://|www\.)[^\s]+$", re.IGNORECASE)

TEMPLATE_PATH = Path(__file__).parent / "template.jpg"
QR_BOX = (188, 542, 509, 863)
LOGO_BOX = (308, 662, 389, 743)
QR_COLOR = (13, 71, 91)

# Придуманный секрет: Telegram присылает его в заголовке,
# чтобы посторонние не могли слать поддельные апдейты.
WEBHOOK_SECRET = "qrbot_wh_7f3a9c2e1b"

_template = None


def get_template() -> Image.Image:
    """Загружаем картинку-шаблон один раз и держим в памяти."""
    global _template
    if _template is None:
        _template = Image.open(TEMPLATE_PATH).convert("RGB")
    return _template


def make_qr_png(data: str) -> bytes:
    """Берём исходный скриншот и меняем в нём только QR-код."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=0,
    )
    qr.add_data(data)
    qr.make(fit=True)
    qr_image = qr.make_image(fill_color=QR_COLOR, back_color="white").convert("RGB")
    qr_image = qr_image.resize(
        (QR_BOX[2] - QR_BOX[0], QR_BOX[3] - QR_BOX[1]), Image.Resampling.NEAREST
    )

    image = get_template().copy()
    logo = image.crop(LOGO_BOX)
    image.paste(qr_image, QR_BOX[:2])
    image.paste(logo, LOGO_BOX[:2])

    buf = io.BytesIO()
    # compress_level=0 — самое быстрое кодирование PNG (важно для лимита CPU).
    image.save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


def normalize_url(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("www."):
        return "https://" + text
    return text


def tg_send_photo(api: str, chat_id, png: bytes, caption: str):
    """Отправляем фото multipart-запросом, собранным вручную."""
    boundary = "----botqrboundary7f3a9c2e"
    crlf = b"\r\n"

    def field(name: str, value: str) -> bytes:
        return (
            ("--" + boundary + "\r\n")
            + ('Content-Disposition: form-data; name="' + name + '"\r\n\r\n')
            + value
            + "\r\n"
        ).encode("utf-8")

    body = b""
    body += field("chat_id", str(chat_id))
    body += field("caption", caption)
    body += (
        "--" + boundary + "\r\n"
        'Content-Disposition: form-data; name="photo"; filename="qr.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("utf-8")
    body += png + crlf
    body += ("--" + boundary + "--\r\n").encode("utf-8")

    return requests.post(
        f"{api}/sendPhoto",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        timeout=60,
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url.split("?", 1)[0]
        path = url.rstrip("/")

        # Одноразовая установка webhook: открой этот адрес в браузере.
        if path.endswith("/setwebhook"):
            return self.set_webhook(url)

        if path.endswith("/health"):
            return Response("ok")

        # Диагностика: /debug?text=... отдаёт готовую картинку ИЛИ текст ошибки.
        if path.endswith("/debug"):
            q = parse_qs(urlparse(request.url).query)
            text = q.get("text", ["https://example.com"])[0]
            try:
                png = make_qr_png(text)
            except Exception as exc:
                return Response(f"error: {exc!r}", status=500)
            return Response(
                to_js(png).buffer, headers={"Content-Type": "image/png"}
            )

        # Проверяем, что запрос действительно от Telegram.
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return Response("forbidden", status=403)

        try:
            update = json.loads(await request.text())
        except Exception:
            return Response("ok")

        try:
            self.handle_update(update)
        except Exception as exc:
            print(f"handle_update error: {exc!r}")
            self.report_error(update, exc)

        return Response("ok")

    def token(self) -> str:
        value = self.env.BOT_TOKEN
        if not value:
            raise RuntimeError("Не задан секрет BOT_TOKEN")
        return str(value)

    def report_error(self, update: dict, exc: Exception) -> None:
        """Пишем ошибку прямо в чат, чтобы её было видно."""
        try:
            msg = update.get("message") or update.get("edited_message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if not chat_id:
                return
            requests.post(
                f"https://api.telegram.org/bot{self.token()}/sendMessage",
                data={"chat_id": chat_id, "text": f"Ошибка: {exc!r}"},
                timeout=30,
            )
        except Exception:
            pass

    def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        api = f"https://api.telegram.org/bot{self.token()}"

        if text.startswith("/start"):
            requests.post(
                f"{api}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": (
                        "Привет! Пришли мне ссылку — я сделаю QR-код.\n\n"
                        "Пример:\nhttps://example.com\n\n"
                        "Также можно отправить любой текст — он тоже станет QR."
                    ),
                },
                timeout=30,
            )
            return

        if text.startswith("/help"):
            requests.post(
                f"{api}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": (
                        "Отправь URL или текст одним сообщением.\n"
                        "В ответ придёт картинка с QR-кодом."
                    ),
                },
                timeout=30,
            )
            return

        if not text:
            requests.post(
                f"{api}/sendMessage",
                data={"chat_id": chat_id, "text": "Пришли непустую ссылку или текст."},
                timeout=30,
            )
            return

        if URL_RE.match(text):
            payload = normalize_url(text)
            caption = f"QR для ссылки:\n{payload}"
        else:
            payload = text
            caption = "QR для текста"

        png = make_qr_png(payload)
        resp = tg_send_photo(api, chat_id, png, caption)
        if resp.status_code != 200:
            raise RuntimeError(f"sendPhoto {resp.status_code}: {resp.text[:300]}")

    def set_webhook(self, request_url: str) -> Response:
        """Ставим webhook на этот же воркер (вызывается один раз)."""
        base = request_url.rstrip("/")
        hook_url = base[: -len("/setwebhook")]
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.token()}/setWebhook",
                data={
                    "url": hook_url,
                    "secret_token": WEBHOOK_SECRET,
                    "drop_pending_updates": "true",
                },
                timeout=30,
            )
        except Exception as exc:
            return Response(f"error: {exc!r}", status=500)
        return Response(resp.text, headers={"Content-Type": "application/json"})