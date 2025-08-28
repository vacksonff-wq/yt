import os, re, time, json, uuid, asyncio, secrets
from typing import Dict, Any, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import jwt  # PyJWT
import uvicorn

# -------------------------- تنظیمات پایه --------------------------
JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_urlsafe(32))
JWT_ALGO = "HS256"
TOKEN_EXP_SECONDS = 12 * 60 * 60  # 12 ساعت

PORT = int(os.getenv("PORT", "8000"))

app = FastAPI(title="WebSocket Chat + Calls (Separated Files)")

# CORS (برای میزبانی جداگانه Front/Back تنظیمات را سختگیرانه‌تر کنید)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# سرو استاتیک
app.mount("/static", StaticFiles(directory="static"), name="static")

# rooms: name -> {"clients": set(WebSocket), "history": [message,...]}
ROOMS: Dict[str, Dict[str, Any]] = {}
ROOMS_LOCK = asyncio.Lock()

# -------------------------- ابزارها --------------------------
def sanitize_room(room: str) -> str:
    room = (room or "lobby").strip().lower()
    room = re.sub(r"[^\w-]+", "", room)
    return room or "lobby"

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
            pass

async def room_users(room_name: str) -> List[Dict[str, str]]:
    users: List[Dict[str, str]] = []
    async with ROOMS_LOCK:
        room = ROOMS.get(room_name)
        if not room:
            return users
        for ws in list(room["clients"]):
            u = ws.scope.get("user_info")
            if u:
                users.append({"id": u["id"], "name": u["name"]})
    return users

async def send_user_list(room_name: str) -> None:
    users = await room_users(room_name)
    await broadcast(room_name, {"type": "user_list", "users": users})

async def relay_to_target(room_name: str, target_id: str, payload: Dict[str, Any]) -> None:
    async with ROOMS_LOCK:
        room = ROOMS.get(room_name)
        if not room:
            return
        for client in list(room["clients"]):
            u = client.scope.get("user_info")
            if u and u.get("id") == target_id:
                try:
                    await send_json(client, payload)
                except Exception:
                    pass
                break

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
    return {"token": token, "room": room, "username": username, "uid": uid, "expSeconds": TOKEN_EXP_SECONDS}

# -------------------------- WebSocket --------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=1008)
        return

    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user = {
            "id": payload["uid"],
            "name": payload["username"],
            "room": payload["room"],
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
        websocket.scope["user_info"] = user  # برای دسترسی سریع به کاربر (عدم استفاده از websocket.user)

    # خوش‌آمد و هیستوری
    await send_json(websocket, {"type": "welcome", "room": user["room"], "username": user["name"], "uid": user["id"]})
    if room["history"]:
        await send_json(websocket, {"type": "history", "messages": room["history"]})

    # اطلاع حضور و ارسال لیست کاربران
    await broadcast(
        user["room"],
        {"type": "presence", "subtype": "join", "user": {"id": user["id"], "name": user["name"]}},
        except_ws=websocket,
    )
    await send_user_list(user["room"])

    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                msg = json.loads(data_text)
            except Exception:
                continue

            mtype = msg.get("type")

            if mtype == "chat" and isinstance(msg.get("text"), str):
                text = msg["text"].strip()
                if not text:
                    continue

                message = {
                    "id": str(uuid.uuid4()),
                    "user": {"id": user["id"], "name": user["name"]},
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
                        room["history"] = room["history"][-50:]

                # پخش برای همه
                await broadcast(user["room"], {"type": "chat", "message": message})

            # سیگنالینگ WebRTC
            elif mtype == "ping":
                # simple keepalive
                try:
                    await send_json(websocket, {"type": "pong", "ts": now_ms()})
                except Exception:
                    pass

            elif mtype in ("call-offer", "call-answer", "ice-candidate", "call-end", "call-decline"):
                target_id = msg.get("target")
                if not target_id:
                    continue
                payload = {
                    "type": mtype,
                    "from": {"id": user["id"], "name": user["name"]},
                    "data": msg.get("data"),
                }
                await relay_to_target(user["room"], target_id, payload)

            elif mtype == "get-users":
                await send_json(websocket, {"type": "user_list", "users": await room_users(user["room"])})

    except WebSocketDisconnect:
        pass
    finally:
        empty = False
        async with ROOMS_LOCK:
            room = ROOMS.get(user["room"])
            if room and websocket in room["clients"]:
                room["clients"].remove(websocket)
                empty = len(room["clients"]) == 0

        await broadcast(
            user["room"],
            {"type": "presence", "subtype": "leave", "user": {"id": user["id"], "name": user["name"]}},
        )
        await send_user_list(user["room"])

        if empty:
            async def cleanup(room_name: str):
                await asyncio.sleep(300)
                async with ROOMS_LOCK:
                    r = ROOMS.get(room_name)
                    if r and len(r["clients"]) == 0:
                        ROOMS.pop(room_name, None)
            asyncio.create_task(cleanup(user["room"]))

# -------------------------- Routes --------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    # ارائه فایل index.html از ریشه پروژه
    return FileResponse("index.html")

@app.get("/health")
async def health():
    return PlainTextResponse("ok")

# -------------------------- اجرا --------------------------
if __name__ == "__main__":
    uvicorn.run("mine:app", host="0.0.0.0", port=PORT, reload=bool(os.getenv("DEV")))