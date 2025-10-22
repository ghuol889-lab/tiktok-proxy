# app.py — FastAPI + yt-dlp
# Эндпоинты:
#   /api?url=... → JSON с прямым .mp4
#   /dl?url=...  → потоковое видео (для Telegram sendVideo)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp
import httpx

app = FastAPI()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)

def pick_format(info: dict) -> str | None:
    """Выбираем лучший mp4 (h264) по высоте и кодеку."""
    fmts = info.get("formats") or []
    def score(f):
        s = 0
        if f.get("ext") == "mp4": s += 10
        v = (f.get("vcodec") or "")
        if v.startswith("avc") or v.startswith("h264"): s += 5
        s += f.get("height") or 0
        return s
    best = max(fmts, key=score, default=None)
    return (best or info).get("url")

def extract(url: str) -> tuple[str | None, str | None]:
    """Достаём прямой видео-URL и заголовок ролика."""
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return pick_format(info), info.get("title")

@app.get("/")
def root():
    return {"ok": True}

@app.get("/api")
def api(url: str):
    try:
        vurl, title = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)
        return {"ok": True, "video_url": vurl, "title": title}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/dl")
async def dl(request: Request, url: str):
    """
    Стримим видео в ответ.
    ВАЖНО: открываем httpx-клиент и поток ВНУТРИ генератора,
    чтобы соединение не схлопывалось (иначе будет 500).
    Никаких попыток читать stream.headers снаружи — это ломает поток.
    """
    try:
        vurl, _ = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)

        req_headers = {"User-Agent": UA, "Referer": "https://www.tiktok.com/"}
        if "range" in request.headers:
            req_headers["Range"] = request.headers["range"]

        async def body():
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                async with client.stream("GET", vurl, headers=req_headers) as r:
                    async for chunk in r.aiter_bytes():
                        yield chunk

        # Контент-тайп можно оставить дефолтным — Telegram это устраивает
        return StreamingResponse(body(), media_type="video/mp4")

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
