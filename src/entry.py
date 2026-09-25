"""Cloudflare Worker (Python) — Telegram-бот QR.

Работает через webhook: Telegram сам присылает сообщение на URL воркера.

Доступ по списку (Cloudflare KV):
  * администратор присылает @username  ->  этому человеку выдаётся доступ;
  * /list             -> показать список доступа;
  * /revoke @username -> забрать доступ;
  * /id               -> узнать свой Telegram ID и username.

Администраторы задаются секретами: ADMIN_IDS (числа) и/или ADMIN_USERNAMES.

На одну ссылку бот присылает ДВА PNG-файла — QR на двух разных шаблонах.
"""

import base64
import io
import json
import re
from urllib.parse import parse_qs, urlparse

import qrcode
import requests
from PIL import Image
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from template_data import (
    TEMPLATE1_B64,
    TEMPLATE1_LOGO,
    TEMPLATE1_QR,
    TEMPLATE2_B64,
    TEMPLATE2_LOGO,
    TEMPLATE2_QR,
)

URL_RE = re.compile(r"^(https?://|www\.)[^\s]+$", re.IGNORECASE)
# @username: 4..32 символа (латиница, цифры, подчёркивание)
USERNAME_RE = re.compile(r"^@([A-Za-z0-9_]{4,32})$")

# Придуманный секрет: Telegram присылает его в заголовке,
# чтобы посторонние не могли слать поддельные апдейты.
WEBHOOK_SECRET = "qrbot_wh_7f3a9c2e1b"

# Ключ в KV-хранилище, где лежит список разрешённых username.
KV_KEY = "allowed"

# Шаблоны: картинка (уже уменьшенная) + области QR и логотипа + цвет квадратиков QR.
TEMPLATE_SPECS = [
    {
        "name": "My Orders",
        "data": TEMPLATE1_B64,
        "qr_box": TEMPLATE1_QR,
        "logo_box": TEMPLATE1_LOGO,
        "qr_color": (0, 0, 0),
    },
    {
        "name": "Payment received",
        "data": TEMPLATE2_B64,
        "qr_box": TEMPLATE2_QR,
        "logo_box": TEMPLATE2_LOGO,
        "qr_color": (13, 71, 91),
    },
]

_templates = None


# --------------------------------------------------------------------------
# вспомогательное
# --------------------------------------------------------------------------

def clean_username(value) -> str:
    """Приводим username к виду 'name' (без @, в нижнем регистре)."""
    if not value:
        return ""
    return str(value).strip().lstrip("@").lower()


def parse_list(value) -> list:
    """Разбираем строку 'a, b, c' в список (для секретов ADMIN_IDS/ADMIN_USERNAMES)."""
    if not value:
        return []
    return [clean_username(x) for x in str(value).split(",") if x.strip()]


def get_templates() -> list:
    """Готовим шаблоны один раз: распаковываем base64 в картинки."""
    global _templates
    if _templates is None:
        _templates = [
            {
                "name": spec["name"],
                "image": Image.open(
                    io.BytesIO(base64.b64decode(spec["data"]))
                ).convert("RGB"),
                "qr_box": spec["qr_box"],
                "logo_box": spec["logo_box"],
                "qr_color": spec["qr_color"],
            }
            for spec in TEMPLATE_SPECS
        ]
    return _templates


def make_qr_png(item: dict, data: str) -> bytes:
    """Рисуем QR на шаблоне, сохраняя логотип в центре."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=0,
    )
    qr.add_data(data)
    qr.make(fit=True)
    n = qr.modules_count  # число квадратиков по стороне
    qr_image = qr.make_image(fill_color=item["qr_color"], back_color="white").convert("RGB")

    box = item["qr_box"]
    bw, bh = box[2] - box[0], box[3] - box[1]
    k = max(1, min(bw, bh) // n)          # целое число пикселей на модуль
    side = n * k                          # чтобы QR был «ровным» и сканировался
    qr_image = qr_image.resize((side, side), Image.Resampling.NEAREST)

    image = item["image"].copy()
    logo = image.crop(item["logo_box"])   # логотип вырезаем ДО затирания

    image.paste((255, 255, 255), box)     # чистим область под QR
    ox = box[0] + (bw - side) // 2
    oy = box[1] + (bh - side) // 2
    image.paste(qr_image, (ox, oy))
    image.paste(logo, item["logo_box"][:2])

    buf = io.BytesIO()
    # compress_level=0 — самое быстрое кодирование PNG (важно для лимита CPU).
    image.save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


def normalize_url(text: str) -> str:
    text = text.strip()
    if text.lower().startswith("www."):
        return "https://" + text
    return text


def tg_send_document(api: str, chat_id, png: bytes, caption: str, filename: str):
    """Отправляем картинку ФАЙЛОМ (sendDocument), чтобы Telegram её не сжимал."""
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
        'Content-Disposition: form-data; name="document"; filename="' + filename + '"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("utf-8")
    body += png + crlf
    body += ("--" + boundary + "--\r\n").encode("utf-8")

    return requests.post(
        f"{api}/sendDocument",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        timeout=60,
    )


# --------------------------------------------------------------------------
# воркер
# --------------------------------------------------------------------------

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url.split("?", 1)[0]
        path = url.rstrip("/")

        # Одноразовая установка webhook: открой этот адрес в браузере.
        if path.endswith("/setwebhook"):
            return self.set_webhook(url)

        if path.endswith("/health"):
            return Response("ok")

        # Диагностика: /debug?text=...&t=1 отдаёт готовую картинку ИЛИ текст ошибки.
        if path.endswith("/debug"):
            q = parse_qs(urlparse(request.url).query)
            text = q.get("text", ["https://example.com"])[0]
            try:
                index = max(1, min(len(get_templates()), int(q.get("t", ["1"])[0])))
            except Exception:
                index = 1
            try:
                png = make_qr_png(get_templates()[index - 1], text)
            except Exception as exc:
                return Response(f"error: {exc!r}", status=500)
            return Response(to_js(png).buffer, headers={"Content-Type": "image/png"})

        # Проверяем, что запрос действительно от Telegram.
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return Response("forbidden", status=403)

        try:
            update = json.loads(await request.text())
        except Exception:
            return Response("ok")

        try:
            await self.handle_update(update)
        except Exception as exc:
            print(f"handle_update error: {exc!r}")
            self.report_error(update, exc)

        return Response("ok")

    # ---------------- Telegram API ----------------

    def env_value(self, name: str, default: str = "") -> str:
        """Безопасно читаем секрет/переменную: незаданный не должен ломать бота."""
        try:
            value = getattr(self.env, name)
        except Exception:
            return default
        if value is None:
            return default
        try:
            text = str(value)
        except Exception:
            return default
        if not text or text in ("undefined", "null"):
            return default
        return text

    def token(self) -> str:
        value = self.env_value("BOT_TOKEN")
        if not value:
            raise RuntimeError("Не задан секрет BOT_TOKEN")
        return value

    def api(self) -> str:
        return f"https://api.telegram.org/bot{self.token()}"

    def send(self, chat_id, text: str) -> None:
        requests.post(
            f"{self.api()}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=30,
        )

    def report_error(self, update: dict, exc: Exception) -> None:
        """Пишем ошибку прямо в чат, чтобы её было видно."""
        try:
            msg = update.get("message") or update.get("edited_message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            if chat_id:
                self.send(chat_id, f"Ошибка: {exc!r}")
        except Exception:
            pass

    # ---------------- доступ ----------------

    def admin_ids(self) -> list:
        return parse_list(self.env_value("ADMIN_IDS"))

    def admin_names(self) -> list:
        return parse_list(self.env_value("ADMIN_USERNAMES"))

    def admins_configured(self) -> bool:
        return bool(self.admin_ids() or self.admin_names())

    def is_admin(self, user: dict) -> bool:
        uid = str(user.get("id", "")).strip()
        uname = clean_username(user.get("username"))
        if uid and uid in self.admin_ids():
            return True
        if uname and uname in self.admin_names():
            return True
        return False

    async def kv_allowed(self):
        """Возвращает (хранилище доступно, список разрешённых username)."""
        try:
            kv = self.env.QRBOT_KV
            raw = await kv.get(KV_KEY)
        except Exception:
            return False, []
        if not raw:
            return True, []
        try:
            data = json.loads(str(raw))
        except Exception:
            return True, []
        if not isinstance(data, list):
            return True, []
        return True, [clean_username(x) for x in data if clean_username(x)]

    async def kv_save(self, names: list) -> bool:
        try:
            kv = self.env.QRBOT_KV
            await kv.put(KV_KEY, json.dumps(names))
            return True
        except Exception:
            return False

    # ---------------- обработка сообщения ----------------

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        user = message.get("from") or {}

        # /id доступна всем — чтобы узнать свой ID для настройки.
        if text.startswith("/id"):
            lines = [f"Ваш ID: {user.get('id')}"]
            uname = clean_username(user.get("username"))
            if uname:
                lines.append(f"Username: @{uname}")
            self.send(chat_id, "\n".join(lines))
            return

        # Первичная настройка: пока не задан администратор — подсказываем шаги.
        if not self.admins_configured():
            self.send(
                chat_id,
                "Бот ещё не настроен.\n"
                f"Ваш ID: {user.get('id')}\n\n"
                "Добавьте секрет ADMIN_IDS с этим ID (или ADMIN_USERNAMES с вашим "
                "@username) в Cloudflare → Settings → Runtime variables and secrets.",
            )
            return

        if text.startswith("/start"):
            if self.is_admin(user):
                self.send(
                    chat_id,
                    "Привет, администратор!\n\n"
                    "• Пришли ссылку — получишь 2 QR-файла.\n"
                    "• Пришли @username — выдашь этому человеку доступ.\n"
                    "• /list — список доступа.\n"
                    "• /revoke @username — забрать доступ.",
                )
            else:
                self.send(chat_id, "Привет! Пришли ссылку — получишь 2 QR-файла.")
            return

        if text.startswith("/help"):
            self.send(chat_id, "Отправь ссылку или текст — придёт 2 QR-файла (два дизайна).")
            return

        # Команды администратора (выдача доступа и т.п.).
        if self.is_admin(user) and await self.admin_commands(chat_id, text):
            return

        # Проверка доступа (администратор проходит всегда).
        if not self.is_admin(user):
            uname = clean_username(user.get("username"))
            available, allowed = await self.kv_allowed()
            if not available:
                self.send(
                    chat_id,
                    "Хранилище доступа недоступно: KV-хранилище не подключено.",
                )
                return
            if not uname:
                self.send(
                    chat_id,
                    "У вас не задан @username в Telegram, поэтому доступ выдать нельзя.",
                )
                return
            if uname not in allowed:
                self.send(
                    chat_id,
                    f"Нет доступа.\nВаш username: @{uname}\n"
                    "Попросите владельца бота выдать доступ.",
                )
                return

        # Генерация QR (два шаблона -> два файла).
        if not text:
            self.send(chat_id, "Пришли ссылку или текст одним сообщением.")
            return

        if URL_RE.match(text):
            payload = normalize_url(text)
            caption = f"QR для ссылки:\n{payload}"
        else:
            payload = text
            caption = "QR для текста"

        for item in get_templates():
            png = make_qr_png(item, payload)
            filename = "qr-" + item["name"].lower().replace(" ", "-") + ".png"
            resp = tg_send_document(self.api(), chat_id, png, caption, filename)
            if resp.status_code != 200:
                raise RuntimeError(f"sendDocument {resp.status_code}: {resp.text[:300]}")

    async def admin_commands(self, chat_id, text: str) -> bool:
        """Обрабатывает команды администратора. True — если сообщение поглощено."""

        if text.startswith("/list"):
            available, allowed = await self.kv_allowed()
            if not available:
                self.send(chat_id, "KV-хранилище не подключено.")
            elif not allowed:
                self.send(chat_id, "Список доступа пуст.")
            else:
                self.send(chat_id, "Доступ:\n" + "\n".join("@" + n for n in allowed))
            return True

        if text.startswith("/revoke") or text.startswith("/remove"):
            parts = text.split()
            if len(parts) < 2:
                self.send(chat_id, "Формат: /revoke @username")
                return True
            name = clean_username(parts[1])
            available, allowed = await self.kv_allowed()
            if not available:
                self.send(chat_id, "KV-хранилище не подключено.")
                return True
            if name in allowed:
                allowed.remove(name)
                await self.kv_save(allowed)
                self.send(chat_id, f"Доступ отозван: @{name}")
            else:
                self.send(chat_id, f"У @{name} и так нет доступа.")
            return True

        match = USERNAME_RE.match(text)
        if match:
            name = clean_username(match.group(1))
            available, allowed = await self.kv_allowed()
            if not available:
                self.send(chat_id, "KV-хранилище не подключено.")
                return True
            if name in allowed:
                self.send(chat_id, f"У @{name} уже есть доступ.")
                return True
            allowed.append(name)
            if await self.kv_save(allowed):
                self.send(chat_id, f"Доступ выдан: @{name}")
            else:
                self.send(chat_id, "Не удалось сохранить доступ.")
            return True

        return False

    # ---------------- служебное ----------------

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