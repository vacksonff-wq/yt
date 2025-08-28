import os, re, time, json, uuid, asyncio, secrets
from typing import Dict, Any, Set, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Header
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import jwt  # PyJWT
import uvicorn

# -------------------------- تنظیمات پایه --------------------------
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGO = "HS256"
TOKEN_EXP_SECONDS = 12 * 60 * 60  # 12 ساعت

PORT = int(os.getenv("PORT", "8000"))

app = FastAPI(title="وب‌چت (WebSocket) — تغییر نام با ذخیره در هاست")

# در صورت میزبانی جدا، CORS را سفت‌تر کنید
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # تغییر دهید برای پروداکشن
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# rooms: name -> {"clients": set(WebSocket), "history": [message,...]}
ROOMS: Dict[str, Dict[str, Any]] = {}
ROOMS_LOCK = asyncio.Lock()

# کاربران: uid -> {"name": str}
USERS_DB_PATH = os.getenv("USERS_DB_PATH", "users_db.json")
USERS: Dict[str, Dict[str, str]] = {}
USERS_LOCK = asyncio.Lock()

# -------------------------- ابزارها --------------------------
def sanitize_room(room: str) -> str:
    room = (room or "lobby").strip().lower()
    room = re.sub(r"[^\w-]+", "", room)
    return room or "lobby"

# اجازه‌ی کاراکترهای حروف/اعداد + فاصله + زیرخط/خط تیره (با پشتیبانی فارسی)
NAME_ALLOWED_PATTERN = re.compile(r"[^\w\-\s\u0600-\u06FF]", re.UNICODE)

def sanitize_name(name: str) -> str:
    name = (name or "").strip()
    # حذف کاراکترهای غیرمجاز
    name = NAME_ALLOWED_PATTERN.sub("", name)
    # فشرده‌سازی فاصله‌ها
    name = re.sub(r"\s+", " ", name)
    # محدودیت طول
    if len(name) < 2:
        name = name + ("_" * (2 - len(name)))
    return name[:32]

def now_ms() -> int:
    return int(time.time() * 1000)

async def send_json(ws: WebSocket, data: Dict[str, Any]) -> None:
    await ws.send_text(json.dumps(data, ensure_ascii=False))

async def broadcast(room_name: str, data: Dict[str, Any], except_ws: WebSocket = None) -> None:
    text = json.dumps(data, ensure_ascii=False)
    async with ROOMS_LOCK:
        room = ROOMS.get(room_name)
        targets: List[WebSocket] = list(room["clients"]) if room else []
    for client in targets:
        if client is except_ws:
            continue
        try:
            await client.send_text(text)
        except Exception:
            # اگر کلاینت اشکال داشت، نادیده بگیر
            pass

# -------------------------- ذخیره/لود کاربران --------------------------
def load_users() -> None:
    global USERS
    try:
        with open(USERS_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                USERS = {str(k): {"name": str(v.get("name", ""))} for k, v in data.items()}
    except FileNotFoundError:
        USERS = {}
    except Exception:
        USERS = {}

def save_users() -> None:
    try:
        with open(USERS_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(USERS, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

load_users()

async def get_username(uid: str, fallback: str) -> str:
    async with USERS_LOCK:
        name = USERS.get(uid, {}).get("name")
        return name or fallback

async def set_username(uid: str, new_name: str) -> None:
    async with USERS_LOCK:
        USERS[uid] = {"name": new_name}
        save_users()

# -------------------------- API: توکن مهمان --------------------------
@app.get("/api/guest-token")
async def guest_token(room: str = "lobby"):
    room = sanitize_room(room)
    uid = str(uuid.uuid4())
    username = "guest-" + uid[:6]
    payload = {
        "uid": uid,
        "username": username,
        "room": room,
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_EXP_SECONDS,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)
    return {"token": token, "room": room, "username": username, "expSeconds": TOKEN_EXP_SECONDS}

# -------------------------- API: تغییر نام --------------------------
@app.post("/api/set-name")
async def set_name(request: Request, authorization: Optional[str] = Header(default=None)):
    # دریافت توکن از هدر Authorization یا query ?token=
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token:
        token = request.query_params.get("token")
    if not token:
        return JSONResponse({"error": "missing token"}, status_code=401)

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        uid = payload["uid"]
        room = sanitize_room(payload.get("room", "lobby"))
    except jwt.ExpiredSignatureError:
        return JSONResponse({"error": "token expired"}, status_code=401)
    except jwt.InvalidTokenError:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    body = await request.json()
    raw_name = str(body.get("name", "")).strip()
    new_name = sanitize_name(raw_name)
    if not new_name:
        return JSONResponse({"error": "invalid name"}, status_code=400)

    # نام قبلی برای اطلاع‌رسانی حضور
    old_name = await get_username(uid, payload.get("username", "user"))

    # ذخیره در هاست (فایل JSON)
    await set_username(uid, new_name)

    # اطلاع تغییر نام در روم
    await broadcast(
        room,
        {
            "type": "presence",
            "subtype": "rename",
            "user": {"id": uid, "old": old_name, "new": new_name},
        },
    )

    return {"ok": True, "uid": uid, "name": new_name}

# -------------------------- WebSocket --------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        # 1008: Policy Violation
        await websocket.close(code=1008)
        return

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        uid = payload["uid"]
        room_name = sanitize_room(payload["room"]) if payload.get("room") else "lobby"
        # نام فعلی کاربر (با اولویت نام ذخیره‌شده در هاست)
        stored_name = await get_username(uid, payload.get("username", "user"))
        user = {
            "id": uid,
            "name": stored_name,
            "room": room_name,
        }
    except jwt.ExpiredSignatureError:
        await websocket.close(code=1008)
        return
    except jwt.InvalidTokenError:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    # پیوستن به روم
    async with ROOMS_LOCK:
        room = ROOMS.get(user["room"])
        if not room:
            room = {"clients": set(), "history": []}
            ROOMS[user["room"]] = room
        room["clients"].add(websocket)

    # خوش‌آمد و هیستوری
    await send_json(websocket, {"type": "welcome", "room": user["room"], "username": user["name"]})
    if room["history"]:
        await send_json(websocket, {"type": "history", "messages": room["history"]})

    # اطلاع حضور
    await broadcast(
        user["room"],
        {"type": "presence", "subtype": "join", "user": {"id": user["id"], "name": user["name"]}},
        except_ws=websocket,
    )

    # حلقه دریافت پیام‌ها
    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                msg = json.loads(data_text)
            except Exception:
                continue

            if msg.get("type") == "chat" and isinstance(msg.get("text"), str):
                text = msg["text"].strip()
                if not text:
                    continue

                # هر بار نام فعلی را از هاست بخوان تا پیام‌ها با نام جدید ثبت شوند
                current_name = await get_username(user["id"], user["name"])  # fallback به نام ورود

                message = {
                    "id": str(uuid.uuid4()),
                    "user": {"id": user["id"], "name": current_name},
                    "text": text,
                    "ts": now_ms(),
                }

                # افزودن به هیستوری
                async with ROOMS_LOCK:
                    room = ROOMS.get(user["room"])
                    if not room:
                        continue
                    room["history"].append(message)
                    if len(room["history"]) > 50:
                        room["history"] = room["history"][ -50: ]

                # پخش برای همه
                await broadcast(user["room"], {"type": "chat", "message": message})

    except WebSocketDisconnect:
        pass
    finally:
        # خروج از روم و اطلاع‌رسانی
        empty = False
        async with ROOMS_LOCK:
            room = ROOMS.get(user["room"])
            if room and websocket in room["clients"]:
                room["clients"].remove(websocket)
                empty = len(room["clients"]) == 0

        # نام فعلی برای اعلان خروج
        final_name = await get_username(user["id"], user["name"]) 

        await broadcast(
            user["room"],
            {"type": "presence", "subtype": "leave", "user": {"id": user["id"], "name": final_name}},
        )

        # پاکسازی تنبل پس از ۵ دقیقه اگر روم خالی ماند
        if empty:
            async def cleanup(room_name: str):
                await asyncio.sleep(300)
                async with ROOMS_LOCK:
                    r = ROOMS.get(room_name)
                    if r and len(r["clients"]) == 0:
                        ROOMS.pop(room_name, None)
            asyncio.create_task(cleanup(user["room"]))

# -------------------------- صفحه‌ی HTML --------------------------
HTML = """<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>وب‌چت (WebSocket)</title>
  <link rel="icon" href="data:,">
  <link rel="stylesheet" href="/styles.css" />
</head>
<body>
  <main class="container">
    <section class="card">
      <header class="toolbar">
        <div class="brand">وب‌چت</div>
        <form id="roomForm" class="room-form">
          <input id="roomInput" type="text" placeholder="نام روم (مثلاً: lobby)" />
          <button type="submit">ورود</button>
        </form>
        <form id="nameForm" class="name-form" autocomplete="off">
          <input id="nameInput" type="text" placeholder="نام نمایشی" maxlength="32" />
          <button type="submit">ذخیره نام</button>
        </form>
      </header>

      <div id="status" class="status">منتظر انتخاب روم…</div>

      <ul id="messages" class="messages"></ul>

      <form id="chatForm" class="chat-form" autocomplete="off">
        <input id="messageInput" type="text" placeholder="پیام خود را بنویسید…" disabled />
        <button id="sendBtn" type="submit" disabled>ارسال</button>
      </form>
    </section>
  </main>

  <script src="/app.js"></script>
</body>
</html>"""

CSS = ":root {
  --bg: #0f1216;
  --card: #151a21;
  --muted: #9aa4b2;
  --text: #e9eef5;
  --accent: #4da3ff;
  --accent-2: #22d3ee;
  --border: #252c36;
  --mine: #1f2937;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial;
  background: radial-gradient(1200px 800px at 80% -10%, rgba(34,211,238,0.08), transparent), var(--bg);
  color: var(--text);
}
.container { display: grid; place-items: center; height: 100%; padding: 24px; }
.card {
  width: min(900px, 100%);
  background: linear-gradient(180deg, rgba(77,163,255,0.06), transparent 200px), var(--card);
  border: 1px solid var(--border);
  border-radius: 18px;
  box-shadow: 0 12px 30px rgba(0,0,0,0.3);
  display: grid;
  grid-template-rows: auto auto 1fr auto;
  overflow: hidden;
}
.toolbar { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.brand {
  font-weight: 700; letter-spacing: 0.2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.room-form { margin-inline-start: auto; display: flex; gap: 8px; }
.room-form input {
  background: #0d1117; border: 1px solid var(--border); color: var(--text);
  padding: 10px 12px; border-radius: 10px; min-width: 180px; outline: none;
}
.name-form { display: flex; gap: 8px; }
.name-form input { background: #0d1117; border: 1px solid var(--border); color: var(--text); padding: 10px 12px; border-radius: 10px; min-width: 160px; outline: none; }
.room-form button, .name-form button, .chat-form button {
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  color: #0b1020; font-weight: 700; border: none; padding: 10px 14px;
  border-radius: 10px; cursor: pointer;
}
.status { padding: 10px 14px; color: var(--muted); font-size: 14px; border-bottom: 1px dashed var(--border); }
.messages {
  list-style: none; margin: 0; padding: 16px; display: flex; flex-direction: column; gap: 12px;
  overflow-y: auto; max-height: 60vh;
}
.msg { background: #121821; border: 1px solid var(--border); border-radius: 14px; padding: 10px 12px; max-width: 80%; }
.msg.mine { margin-inline-start: auto; background: var(--mine); }
.msg .meta { font-size: 12px; color: var(--muted); display: flex; gap: 8px; margin-bottom: 4px; }
.msg .user { font-weight: 600; }
.msg .text { white-space: pre-wrap; word-break: break-word; }
.chat-form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--border); background: #0b0f14; }
.chat-form input {
  flex: 1; background: #0d1117; border: 1px solid var(--border); color: var(--text);
  padding: 12px; border-radius: 10px; outline: none;
}
"

JS = """(() => {
  const $ = (sel) => document.querySelector(sel);

  const statusEl = '#status';
  const msgsEl = '#messages';
  const roomForm = '#roomForm';
  const roomInput = '#roomInput';
  const nameForm = '#nameForm';
  const nameInput = '#nameInput';
  const chatForm = '#chatForm';
  const msgInput = '#messageInput';
  const sendBtn = '#sendBtn';

  // Cache DOM once
  const DOM = new Map([
    [statusEl, document.querySelector(statusEl)],
    [msgsEl, document.querySelector(msgsEl)],
    [roomForm, document.querySelector(roomForm)],
    [roomInput, document.querySelector(roomInput)],
    [nameForm, document.querySelector(nameForm)],
    [nameInput, document.querySelector(nameInput)],
    [chatForm, document.querySelector(chatForm)],
    [msgInput, document.querySelector(msgInput)],
    [sendBtn, document.querySelector(sendBtn)],
  ]);

  let ws = null;
  let me = { username: null, room: null, token: null };

  function pushMessage({ id, user, text, ts }) {
    const li = document.createElement('li');
    li.className = user && user.name === me.username ? 'msg mine' : 'msg';
    const time = new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    li.innerHTML = `
      <div class="meta">
        <span class="user">${user?.name || 'سیستم'}</span>
        <span class="time">${time}</span>
      </div>
      <div class="text"></div>
    `;
    li.querySelector('.text').textContent = text;
    DOM.get(msgsEl).appendChild(li);
    DOM.get(msgsEl).scrollTop = DOM.get(msgsEl).scrollHeight;
  }

  function pushSystem(text) {
    pushMessage({ id: crypto.randomUUID(), user: null, text, ts: Date.now() });
  }

  async function joinRoom(roomName) {
    const res = await fetch(`/api/guest-token?room=${encodeURIComponent(roomName)}`);
    const data = await res.json();
    me.username = data.username;
    me.room = data.room;
    me.token = data.token;

    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const wsUrl = `${protocol}://${location.host}/ws?token=${encodeURIComponent(me.token)}`;

    DOM.get(statusEl).textContent = `در حال اتصال به روم «${me.room}»…`;
    ws = new WebSocket(wsUrl);

    ws.addEventListener('open', () => {
      DOM.get(statusEl).textContent = `وصل شد. نام شما: ${me.username} | روم: ${me.room}`;
      DOM.get(msgInput).disabled = false;
      DOM.get(sendBtn).disabled = false;
      DOM.get(msgInput).focus();
    });

    ws.addEventListener('message', (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch { return; }

      if (payload.type === 'welcome') {
        // سنکرون با نام فعلی سرور (ممکن است قبلاً ذخیره شده باشد)
        if (payload.username) {
          me.username = payload.username;
          DOM.get(statusEl).textContent = `وصل شد. نام شما: ${me.username} | روم: ${me.room}`;
        }
        pushSystem(`به روم «${payload.room}» خوش آمدید.`);
      } else if (payload.type === 'history') {
        (payload.messages || []).forEach((m) => pushMessage(m));
      } else if (payload.type === 'presence') {
        if (payload.subtype === 'join') pushSystem(`👋 ${payload.user.name} وارد شد.`);
        if (payload.subtype === 'leave') pushSystem(`👋 ${payload.user.name} خارج شد.`);
        if (payload.subtype === 'rename') pushSystem(`✏️ ${payload.user.old} نام خود را به «${payload.user.new}» تغییر داد.`);
      } else if (payload.type === 'chat') {
        pushMessage(payload.message);
      }
    });

    ws.addEventListener('close', () => {
      DOM.get(statusEl).textContent = 'اتصال بسته شد.';
      DOM.get(msgInput).disabled = true;
      DOM.get(sendBtn).disabled = true;
    });

    ws.addEventListener('error', () => {
      DOM.get(statusEl).textContent = 'خطای اتصال.';
    });
  }

  DOM.get(roomForm).addEventListener('submit', (e) => {
    e.preventDefault();
    const roomName = (DOM.get(roomInput).value || 'lobby').trim();
    if (!roomName) return;
    if (ws && ws.readyState === WebSocket.OPEN) ws.close(1000, 'switching room');
    DOM.get(msgsEl).innerHTML = '';
    joinRoom(roomName);
  });

  DOM.get(nameForm).addEventListener('submit', async (e) => {
    e.preventDefault();
    const desired = (DOM.get(nameInput).value || '').trim();
    if (!desired || !me.token) return;

    const res = await fetch('/api/set-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${me.token}` },
      body: JSON.stringify({ name: desired })
    });

    const data = await res.json().catch(() => ({}));
    if (res.ok && data?.name) {
      me.username = data.name;
      DOM.get(statusEl).textContent = `نام شما ذخیره شد: ${me.username} | روم: ${me.room}`;
      DOM.get(nameInput).value = '';
    } else {
      pushSystem(`❗️ خطا در ذخیره نام: ${data?.error || 'نامعتبر'}`);
    }
  });

  DOM.get(chatForm).addEventListener('submit', (e) => {
    e.preventDefault();
    const text = DOM.get(msgInput).value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: 'chat', text }));
    DOM.get(msgInput).value = '';
    DOM.get(msgInput).focus();
  });

  const params = new URLSearchParams(location.search);
  const initRoom = params.get('room');
  if (initRoom) {
    DOM.get(roomInput).value = initRoom;
    DOM.get(roomForm).requestSubmit();
  }
})();"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML

@app.get("/styles.css")
async def styles():
    return Response(CSS, media_type="text/css")

@app.get("/app.js")
async def app_js():
    return Response(JS, media_type="application/javascript")

@app.get("/health")
async def health():
    return PlainTextResponse("ok")

# -------------------------- اجرا --------------------------
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=bool(os.getenv("DEV")))
