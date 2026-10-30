"""LoL 리모컨 - PC 에이전트

롤 클라이언트(LCU)에 접속해서 폰에서 아래 기능을 조작할 수 있게 해주는 로컬 서버입니다.
  - 로비 만들기 / 포지션 선택 / 큐 시작·취소 / 수락·거절 / 자동 수락
  - 챔피언 선택 (호버, 픽 확정, 밴)
  - 소환사 주문
  - 룬 페이지 (기존 페이지 사용, 새로 만들기, 덮어쓰기)

실행:  python lol_agent.py
"""
import asyncio
import base64
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import httpx
import psutil
import uvicorn
from fastapi import (Depends, FastAPI, Header, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

try:
    import qrcode
    import qrcode.image.svg
except ImportError:  # pip install qrcode
    qrcode = None

PORT = int(os.environ.get("LOL_REMOTE_PORT", "8765"))
# exe로 묶이면 리소스(index.html)가 임시 폴더(_MEIPASS)에 풀린다.
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# 토큰 같은 설정이 저장되는 곳 (윈도우: %APPDATA%\LoLRemote)
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "LoLRemote"


def load_token() -> str:
    """폰에서 접속할 때 쓰는 비밀 토큰. 한 번 만들면 저장해 두고 계속 쓴다(폰을 다시 연결할 필요 없음)."""
    env = os.environ.get("LOL_REMOTE_TOKEN")
    if env:
        return env
    f = APP_DIR / "token.txt"
    try:
        if f.is_file() and f.read_text().strip():
            return f.read_text().strip()
        APP_DIR.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(8)
        f.write_text(token)
        return token
    except OSError:
        return secrets.token_urlsafe(8)


TOKEN = load_token()
_paired = {"done": (APP_DIR / "paired").exists()}


def mark_paired():
    """폰이 한 번이라도 연결되면 기록해서, 다음부터는 QR 창을 자동으로 띄우지 않는다."""
    if not _paired["done"]:
        _paired["done"] = True
        try:
            APP_DIR.mkdir(parents=True, exist_ok=True)
            (APP_DIR / "paired").write_text("1")
        except OSError:
            pass


def reset_token():
    """연결 초기화: 새 토큰을 발급해서 기존에 연결된 폰은 접속할 수 없게 한다."""
    global TOKEN
    TOKEN = secrets.token_urlsafe(8)
    _paired["done"] = False
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        (APP_DIR / "token.txt").write_text(TOKEN)
        (APP_DIR / "paired").unlink(missing_ok=True)
    except OSError:
        pass

LOCKFILE_CANDIDATES = [
    os.environ.get("LOL_LOCKFILE", ""),
    r"C:\Riot Games\League of Legends\lockfile",
]


# ============================================================ LCU 접속
def find_credentials():
    """실행 중인 클라이언트 프로세스 인자에서 포트/토큰을 찾고, 실패하면 lockfile을 읽는다."""
    for proc in psutil.process_iter(["name", "cmdline"]):
        if (proc.info["name"] or "").lower().startswith("leagueclientux"):
            cmd = " ".join(proc.info["cmdline"] or [])
            port = re.search(r"--app-port=(\d+)", cmd)
            token = re.search(r"--remoting-auth-token=([\w-]+)", cmd)
            if port and token:
                return int(port[1]), token[1]

    for path in LOCKFILE_CANDIDATES:
        if path and Path(path).is_file():
            try:
                # 형식: 이름:PID:포트:비밀번호:프로토콜
                _, _, port, password, _ = Path(path).read_text().split(":")
                return int(port), password
            except ValueError:
                pass
    return None


class LCU:
    def __init__(self):
        self.http: httpx.AsyncClient | None = None

    async def _ensure(self):
        if self.http is None:
            creds = find_credentials()
            if not creds:
                raise HTTPException(503, "롤 클라이언트를 찾을 수 없어요. 클라이언트가 켜져 있는지 확인하세요.")
            port, password = creds
            self.http = httpx.AsyncClient(
                base_url=f"https://127.0.0.1:{port}",
                auth=("riot", password),
                verify=False,  # LCU는 자체 서명 인증서를 씀
                timeout=5,
            )

    async def call(self, method: str, path: str, **kwargs) -> httpx.Response:
        await self._ensure()
        try:
            return await self.http.request(method, path, **kwargs)
        except httpx.HTTPError:
            # 클라이언트가 재시작되면 포트/토큰이 바뀌므로 다음 호출에서 다시 찾는다.
            http, self.http = self.http, None
            await http.aclose()
            raise HTTPException(503, "클라이언트 연결이 끊겼어요. 잠시 후 다시 시도해 주세요.")

    async def json(self, method: str, path: str, default=None, **kwargs):
        """성공하면 JSON, 실패(404 등)하면 default를 돌려준다."""
        resp = await self.call(method, path, **kwargs)
        if not resp.is_success:
            return default
        try:
            return resp.json()
        except ValueError:
            return default


lcu = LCU()


def check(resp: httpx.Response):
    if not resp.is_success:
        try:
            msg = resp.json().get("message", "")
        except Exception:
            msg = resp.text
        raise HTTPException(400, msg or f"LCU 오류 {resp.status_code}")
    return {"ok": True}


# ============================================================ 인증
def ok_token(value: str) -> bool:
    return secrets.compare_digest(value.encode(), TOKEN.encode())


def auth(x_token: str = Header(""), token: str = Query("")):
    # 일반 API는 헤더로, 이미지(<img>)처럼 헤더를 못 붙이는 경우는 쿼리로 받는다.
    if not ok_token(x_token or token):
        raise HTTPException(401, "토큰이 올바르지 않아요. PC의 QR로 다시 연결하세요.")
    mark_paired()


# ============================================================ 챔프 선택 요약
def summarize_session(s: dict) -> dict:
    me = s.get("localPlayerCellId")
    mine = next((p for p in s.get("myTeam", []) if p.get("cellId") == me), {})
    actions = [a for group in s.get("actions", []) for a in group]
    pending = [a for a in actions if a.get("actorCellId") == me and not a.get("completed")]

    def find(kind):
        for a in pending:
            if a.get("type") == kind:
                return {"id": a["id"], "champion_id": a.get("championId", 0),
                        "in_progress": bool(a.get("isInProgress"))}
        return None

    timer = s.get("timer", {})
    return {
        "phase": timer.get("phase"),
        "seconds_left": int(timer.get("adjustedTimeLeftInPhase", 0) / 1000),
        "pick": find("pick"),
        "ban": find("ban"),
        "my_champion": mine.get("championId") or mine.get("championPickIntent") or 0,
        "spell1": mine.get("spell1Id"),
        "spell2": mine.get("spell2Id"),
        "position": mine.get("assignedPosition", ""),
    }


# ============================================================ 실시간 이벤트 / 자동 수락
auto = {"accept": False}
clients: set[WebSocket] = set()
WATCH = (
    "/lol-gameflow/v1/gameflow-phase",
    "/lol-champ-select/v1/session",
    "/lol-matchmaking/v1/",
    "/lol-lobby/v2/lobby",
    "/lol-perks/v1/currentpage",
    "/lol-perks/v1/pages",
)


async def broadcast(text: str):
    for ws in list(clients):
        try:
            await ws.send_text(text)
        except Exception:
            clients.discard(ws)


async def lcu_events_loop():
    """LCU WebSocket을 구독하고, 관련 이벤트가 오면 폰에 'changed' 신호를 보낸다."""
    while True:
        creds = find_credentials()
        if not creds:
            await asyncio.sleep(3)
            continue
        port, password = creds
        basic = base64.b64encode(f"riot:{password}".encode()).decode()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    f"wss://127.0.0.1:{port}/",
                    headers={"Authorization": f"Basic {basic}"},
                    ssl=False,
                    heartbeat=30,
                ) as ws:
                    await ws.send_str(json.dumps([5, "OnJsonApiEvent"]))
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT or not msg.data:
                            continue
                        try:
                            _, _, event = json.loads(msg.data)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(event, dict) and str(event.get("uri", "")).startswith(WATCH):
                            await broadcast("changed")
        except Exception:
            pass
        await asyncio.sleep(3)


async def auto_accept_loop():
    while True:
        await asyncio.sleep(1)
        if not auto["accept"]:
            continue
        try:
            phase = await lcu.json("GET", "/lol-gameflow/v1/gameflow-phase")
            if phase == "ReadyCheck":
                rc = await lcu.json("GET", "/lol-matchmaking/v1/ready-check", {})
                # 아직 응답 전일 때만 수락 (직접 거절한 매치를 다시 수락하지 않도록)
                if rc.get("playerResponse") == "None":
                    await lcu.call("POST", "/lol-matchmaking/v1/ready-check/accept")
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [asyncio.create_task(lcu_events_loop()), asyncio.create_task(auto_accept_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str = Query("")):
    if not ok_token(token):
        await ws.close(code=1008)
        return
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


# ============================================================ 요청 모델
class LobbyReq(BaseModel):
    queue_id: int


class PositionReq(BaseModel):
    first: str = "FILL"
    second: str = "FILL"


class AutoReq(BaseModel):
    enabled: bool


class SelectReq(BaseModel):
    kind: str  # "pick" | "ban"
    champion_id: int
    lock: bool = False


class SpellReq(BaseModel):
    spell1: int
    spell2: int


class PageReq(BaseModel):
    page_id: int


class RuneReq(BaseModel):
    name: str = "LoL 리모컨"
    primary: int
    sub: int
    perks: list[int]
    page_id: int | None = None


# ============================================================ 상태
@app.get("/api/state", dependencies=[Depends(auth)])
async def get_state():
    phase = await lcu.json("GET", "/lol-gameflow/v1/gameflow-phase", "None")
    me = await lcu.json("GET", "/lol-summoner/v1/current-summoner", {})
    out = {
        "phase": phase,
        "summoner": me.get("gameName") or me.get("displayName"),
        "auto_accept": auto["accept"],
    }

    if phase in ("Lobby", "Matchmaking", "ReadyCheck"):
        lobby = await lcu.json("GET", "/lol-lobby/v2/lobby")
        if lobby:
            local = lobby.get("localMember") or {}
            out["lobby"] = {
                "queue_id": (lobby.get("gameConfig") or {}).get("queueId"),
                "can_start": lobby.get("canStartActivity"),
                "first": local.get("firstPositionPreference", ""),
                "second": local.get("secondPositionPreference", ""),
            }

    if phase == "Matchmaking":
        search = await lcu.json("GET", "/lol-matchmaking/v1/search")
        if search:
            out["queue"] = {
                "elapsed": search.get("timeInQueue", 0),
                "estimated": search.get("estimatedQueueTime", 0),
            }

    if phase == "ChampSelect":
        session = await lcu.json("GET", "/lol-champ-select/v1/session")
        if session:
            cs = summarize_session(session)
            cs["pickable"] = await lcu.json("GET", "/lol-champ-select/v1/pickable-champion-ids", [])
            cs["bannable"] = await lcu.json("GET", "/lol-champ-select/v1/bannable-champion-ids", [])
            out["cs"] = cs
    return out


# ============================================================ 큐 / 로비
@app.post("/api/lobby", dependencies=[Depends(auth)])
async def create_lobby(req: LobbyReq):
    return check(await lcu.call("POST", "/lol-lobby/v2/lobby", json={"queueId": req.queue_id}))


@app.post("/api/lobby/leave", dependencies=[Depends(auth)])
async def leave_lobby():
    return check(await lcu.call("DELETE", "/lol-lobby/v2/lobby"))


@app.post("/api/lobby/positions", dependencies=[Depends(auth)])
async def set_positions(req: PositionReq):
    return check(await lcu.call(
        "PUT", "/lol-lobby/v2/lobby/members/localMember/position-preferences",
        json={"firstPreference": req.first, "secondPreference": req.second}))


@app.post("/api/queue/start", dependencies=[Depends(auth)])
async def queue_start():
    return check(await lcu.call("POST", "/lol-lobby/v2/lobby/matchmaking/search"))


@app.post("/api/queue/cancel", dependencies=[Depends(auth)])
async def queue_cancel():
    return check(await lcu.call("DELETE", "/lol-lobby/v2/lobby/matchmaking/search"))


@app.post("/api/accept", dependencies=[Depends(auth)])
async def accept():
    return check(await lcu.call("POST", "/lol-matchmaking/v1/ready-check/accept"))


@app.post("/api/decline", dependencies=[Depends(auth)])
async def decline():
    return check(await lcu.call("POST", "/lol-matchmaking/v1/ready-check/decline"))


@app.post("/api/auto-accept", dependencies=[Depends(auth)])
async def set_auto_accept(req: AutoReq):
    auto["accept"] = req.enabled
    return {"ok": True, "auto_accept": req.enabled}


# ============================================================ 챔피언 선택
_champ_cache = {"t": 0.0, "data": []}


@app.get("/api/champions", dependencies=[Depends(auth)])
async def champions():
    if _champ_cache["data"] and time.time() - _champ_cache["t"] < 60:
        return _champ_cache["data"]
    me = await lcu.json("GET", "/lol-summoner/v1/current-summoner", {})
    sid = me.get("summonerId")
    if sid is None:
        raise HTTPException(503, "소환사 정보를 불러오지 못했어요. 클라이언트 로그인을 확인하세요.")
    raw = await lcu.json("GET", f"/lol-champions/v1/inventories/{sid}/champions-minimal", [])
    out = []
    for c in raw:
        if c.get("id", -1) <= 0:
            continue
        own = c.get("ownership") or {}
        owned = bool(own.get("owned") or c.get("freeToPlay") or (own.get("rental") or {}).get("rented"))
        out.append({
            "id": c["id"],
            "name": c.get("name", ""),
            "alias": c.get("alias", ""),
            "owned": owned,
            "icon": c.get("squarePortraitPath") or f"/lol-game-data/assets/v1/champion-icons/{c['id']}.png",
        })
    out.sort(key=lambda c: c["name"])
    _champ_cache.update(t=time.time(), data=out)
    return out


@app.post("/api/champselect/select", dependencies=[Depends(auth)])
async def champselect_select(req: SelectReq):
    if req.kind not in ("pick", "ban"):
        raise HTTPException(400, "kind는 pick 또는 ban 이어야 해요.")
    session = await lcu.json("GET", "/lol-champ-select/v1/session")
    if not session:
        raise HTTPException(400, "챔피언 선택 중이 아니에요.")
    action = summarize_session(session)[req.kind]
    if not action:
        raise HTTPException(400, "지금은 할 수 있는 행동이 없어요.")
    if (req.lock or req.kind == "ban") and not action["in_progress"]:
        raise HTTPException(400, "아직 내 차례가 아니에요." + (" (픽은 차례 전에 호버만 가능해요)" if req.kind == "pick" else ""))
    body = {"championId": req.champion_id}
    if req.lock:
        body["completed"] = True
    return check(await lcu.call("PATCH", f"/lol-champ-select/v1/session/actions/{action['id']}", json=body))


# ============================================================ 소환사 주문
FALLBACK_SPELLS = [(1, "정화"), (3, "탈진"), (4, "점멸"), (6, "유체화"), (7, "회복"), (11, "강타"),
                   (12, "순간이동"), (13, "총명"), (14, "점화"), (21, "방어막"), (32, "표식")]


@app.get("/api/spells", dependencies=[Depends(auth)])
async def spells_list():
    raw = await lcu.json("GET", "/lol-game-data/assets/v1/summoner-spells.json", [])
    out = [{"id": s["id"], "name": s["name"], "icon": s.get("iconPath", "")}
           for s in raw if {"CLASSIC", "ARAM"} & set(s.get("gameModes", []))]
    out.sort(key=lambda s: s["name"])
    return out or [{"id": i, "name": n, "icon": ""} for i, n in FALLBACK_SPELLS]


@app.post("/api/spells", dependencies=[Depends(auth)])
async def spells_set(req: SpellReq):
    if req.spell1 == req.spell2:
        raise HTTPException(400, "서로 다른 주문 2개를 골라 주세요.")
    return check(await lcu.call("PATCH", "/lol-champ-select/v1/session/my-selection",
                                json={"spell1Id": req.spell1, "spell2Id": req.spell2}))


# ============================================================ 룬
# 클라이언트가 능력치 파편 정보를 주지 않을 때 쓰는 값. 시즌/패치로 바뀔 수 있어요.
FALLBACK_SHARDS = [[5008, 5005, 5007], [5008, 5010, 5001], [5011, 5013, 5001]]
_rune_cache: dict = {}


@app.get("/api/runes/data", dependencies=[Depends(auth)])
async def runes_data():
    if _rune_cache:
        return _rune_cache
    styles = await lcu.json("GET", "/lol-perks/v1/styles", [])
    perks = await lcu.json("GET", "/lol-perks/v1/perks", [])
    perk_map = {p["id"]: {"name": p.get("name", ""), "icon": p.get("iconPath", "")} for p in perks}

    out_styles, shards = [], None
    for s in styles:
        slots = s.get("slots", [])
        keystones = [x for x in slots if x.get("type") == "kKeyStone"]
        mods = [x["perks"] for x in slots if x.get("type") == "kStatMod"]
        rows = [x["perks"] for x in slots if x.get("type") not in ("kKeyStone", "kStatMod")]
        if not keystones or len(rows) < 3:
            continue
        if mods and shards is None and len(mods) >= 3:
            shards = mods[:3]
        out_styles.append({
            "id": s["id"], "name": s.get("name", ""), "icon": s.get("iconPath", ""),
            "keystones": keystones[0]["perks"], "rows": rows[:3],
        })
    if not out_styles:
        raise HTTPException(503, "룬 데이터를 불러오지 못했어요.")
    if shards is None:
        shards = [[i for i in row if i in perk_map] for row in FALLBACK_SHARDS]
    _rune_cache.update(styles=out_styles, perks=perk_map, shards=shards)
    return _rune_cache


@app.get("/api/runes/pages", dependencies=[Depends(auth)])
async def runes_pages():
    pages = await lcu.json("GET", "/lol-perks/v1/pages", [])
    return [{
        "id": p["id"], "name": p.get("name", ""), "active": bool(p.get("isActive")),
        "editable": bool(p.get("isEditable")), "primary": p.get("primaryStyleId"),
        "sub": p.get("subStyleId"), "perks": p.get("selectedPerkIds", []),
    } for p in pages]


@app.post("/api/runes/activate", dependencies=[Depends(auth)])
async def runes_activate(req: PageReq):
    return check(await lcu.call("PUT", "/lol-perks/v1/currentpage", json=req.page_id))


@app.post("/api/runes/apply", dependencies=[Depends(auth)])
async def runes_apply(req: RuneReq):
    if len(req.perks) != 9:
        raise HTTPException(400, "룬은 9개(핵심 1 + 메인 3 + 보조 2 + 능력치 3)를 모두 골라야 해요.")
    if req.primary == req.sub:
        raise HTTPException(400, "메인과 보조 룬 계열이 같을 수 없어요.")
    body = {
        "name": (req.name or "LoL 리모컨")[:25],
        "primaryStyleId": req.primary,
        "subStyleId": req.sub,
        "selectedPerkIds": req.perks,
        "current": True,
    }
    pages = await lcu.json("GET", "/lol-perks/v1/pages", [])

    if req.page_id is not None:  # 지정한 페이지에 덮어쓰기
        target = next((p for p in pages if p["id"] == req.page_id and p.get("isEditable")), None)
        if not target:
            raise HTTPException(400, "수정할 수 없는 룬 페이지예요.")
    else:  # 같은 이름의 페이지가 있으면 그걸 갱신, 없으면 새로 만든다
        target = next((p for p in pages if p.get("name") == body["name"] and p.get("isEditable")), None)

    if target:
        check(await lcu.call("PUT", f"/lol-perks/v1/pages/{target['id']}", json=body))
        await lcu.call("PUT", "/lol-perks/v1/currentpage", json=target["id"])
        return {"ok": True}

    resp = await lcu.call("POST", "/lol-perks/v1/pages", json=body)
    if not resp.is_success:
        raise HTTPException(400, "룬 페이지를 새로 만들 수 없어요(페이지가 가득 찼을 수 있어요). "
                                 "룬 페이지 목록에서 덮어쓸 페이지를 골라 주세요.")
    return {"ok": True}


# ============================================================ 이미지 프록시 / 페이지
@app.get("/api/asset", dependencies=[Depends(auth)])
async def asset(path: str):
    """폰은 LCU에 직접 접근할 수 없어서 아이콘을 에이전트가 대신 가져온다."""
    if not path.startswith("/lol-game-data/assets/") or ".." in path:
        raise HTTPException(400, "허용되지 않는 경로예요.")
    resp = await lcu.call("GET", path)
    if not resp.is_success:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    return Response(resp.content,
                    media_type=resp.headers.get("content-type", "image/png"),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


# ============================================================ 실행
def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def connect_url() -> str:
    return f"http://{local_ip()}:{PORT}/?token={TOKEN}"


@app.get("/qr", response_class=HTMLResponse)
async def qr_page(request: Request):
    """PC 브라우저에서 보는 QR 페이지. 토큰이 들어 있어서 PC 자신(localhost)만 열 수 있다."""
    if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "이 PC에서만 열 수 있는 페이지예요.")
    if qrcode is None:
        return HTMLResponse("<meta charset='utf-8'>QR 라이브러리가 없어요. <code>pip install qrcode</code> 후 다시 실행하세요.")
    url = connect_url()
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = re.sub(r"^<\?xml[^>]*\?>", "", buf.getvalue().decode()).strip()
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>LoL 리모컨 QR</title>
<style>body{{background:#0a1428;color:#f0e6d2;font-family:system-ui,sans-serif;text-align:center;padding:32px}}
.qr{{background:#fff;display:inline-block;padding:12px;border-radius:12px}}.qr svg{{width:320px;height:320px;display:block}}
code{{display:block;margin-top:16px;color:#c8aa6e;word-break:break-all}}</style></head><body>
<h2>폰 카메라로 QR을 스캔하세요</h2><p>PC와 같은 와이파이에 연결돼 있어야 해요.</p>
<div class="qr">{svg}</div><code>{url}</code></body></html>"""


def port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def open_qr():
    webbrowser.open(f"http://127.0.0.1:{PORT}/qr")


def make_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (10, 20, 40, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), outline=(200, 170, 110, 255), width=6)
    d.ellipse((26, 26, 38, 38), fill=(200, 170, 110, 255))
    return img


# ---- 윈도우 시작 시 자동 실행 (레지스트리 Run 키)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "LoLRemote")
            return True
    except Exception:
        return False


def set_autostart(enable: bool):
    import winreg
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, "LoLRemote", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "LoLRemote")
            except FileNotFoundError:
                pass


def run_tray(server: uvicorn.Server):
    import pystray

    def on_qr(icon, item):
        open_qr()

    def on_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def on_reset(icon, item):
        reset_token()
        open_qr()

    def on_quit(icon, item):
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("폰 연결 (QR 보기)", on_qr, default=True),
        pystray.MenuItem("윈도우 시작 시 자동 실행", on_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("연결 초기화 (새 QR 발급)", on_reset),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", on_quit),
    )
    pystray.Icon("LoLRemote", make_icon(), "LoL 리모컨", menu).run()


def main():
    if port_in_use():  # 이미 실행 중이면 새로 띄우지 않고 QR만 다시 보여준다
        open_qr()
        return

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if sys.stdout:  # 콘솔 창이 있을 때(python으로 직접 실행)만 출력
        url = connect_url()
        print("\n폰 카메라로 QR을 스캔하거나, 아래 주소를 폰 브라우저에 입력하세요 (PC와 같은 와이파이):")
        print(f"  {url}\n")
        if qrcode:
            try:
                qr = qrcode.QRCode(border=1)
                qr.add_data(url)
                qr.print_ascii(invert=True)
            except Exception:
                pass
    if not _paired["done"] and not os.environ.get("LOL_REMOTE_NO_BROWSER"):
        threading.Timer(1.5, open_qr).start()  # 처음 한 번만 QR 창을 자동으로 연다

    try:
        run_tray(server)  # 작업 표시줄 트레이 아이콘 (종료할 때까지 여기서 대기)
    except ImportError:
        thread.join()  # pystray가 없으면 콘솔 모드로 계속 실행


if __name__ == "__main__":
    main()
