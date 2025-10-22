# app.py — FastAPI + yt-dlp: /api возвращает прямой mp4, /dl стримит видео (для Telegram)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp, httpx

app = FastAPI()
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def pick_format(info: dict) -> str | None:
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
    ydl_opts = {
        "quiet": True, "noplaylist": True, "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return pick_format(info), info.get("title")

@app.get("/api")
def api(url: str):
    vurl, title = extract(url)
    if not vurl:
        return JSONResponse({"ok": False}, status_code=404)
    return {"ok": True, "video_url": vurl, "title": title}

@app.get("/dl")
async def dl(request: Request, url: str):
    vurl, _ = extract(url)
    if not vurl:
        return JSONResponse({"ok": False}, status_code=404)

    headers = {"User-Agent": UA, "Referer": "https://www.tiktok.com/"}
    # поддержка range-запросов (Telegram это любит)
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        upstream = await client.stream("GET", vurl, headers=headers)

        async def gen():
            async for chunk in upstream.aiter_bytes():
                yield chunk

        resp = StreamingResponse(gen(),
                                 status_code=upstream.status_code,
                                 media_type=upstream.headers.get("content-type", "video/mp4"))
        # важные заголовки для корректного стриминга
        for h in ["content-length", "accept-ranges", "content-range", "etag", "last-modified"]:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
