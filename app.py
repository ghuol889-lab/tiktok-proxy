# app.py — FastAPI + yt-dlp
# /api?url=... → JSON (проксированный URL через воркер)
# /dl?url=...  → поток через воркер (200/206 + Range), читабельное имя файла
# ВАЖНО: задайте переменную окружения PROXY_BASE = https://<имя>.workers.dev

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response
import yt_dlp
import httpx
import os
import re
from urllib.parse import quote as urlquote

app = FastAPI()

PROXY_BASE = os.getenv("PROXY_BASE", "").rstrip("/")
if not PROXY_BASE:
    raise RuntimeError('Set env var PROXY_BASE = "https://<name>.workers.dev"')

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,ru-RU;q=0.8,ru;q=0.7",
    "Connection": "keep-alive",
}

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

def extract(url: str):
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {
            "User-Agent": UA,
            "Referer": "https://www.tiktok.com/",
            "Origin": "https://www.tiktok.com",
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    vurl = pick_format(info)
    title = info.get("title")
    return vurl, title

def safe_filename(title: str | None) -> tuple[str, str]:
    base = (title or "video").strip()
    base = re.sub(r'[\\/*?:"<>|]+', "_", base)
    base = re.sub(r"\s+", " ", base).strip()[:80] or "video"
    fn_utf8 = f"{base}.mp4"
    fn_ascii = fn_utf8.encode("ascii", "ignore").decode("ascii") or "video.mp4"
    return fn_ascii, fn_utf8

def proxied(url: str) -> str:
    return f"{PROXY_BASE}/tproxy?u={urlquote(url, safe='')}"

@app.get("/")
def root():
    return {"ok": True, "proxy": PROXY_BASE}

@app.get("/api")
def api(url: str):
    try:
        vurl, title = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)
        return {"ok": True, "video_url": proxied(vurl), "title": title}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/dl")
async def dl(request: Request, url: str):
    try:
        vurl, title = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)

        purl = proxied(vurl)

        headers = dict(BASE_HEADERS)
        if "range" in request.headers:
            headers["Range"] = request.headers["range"]

        async with httpx.AsyncClient(follow_redirects=True, timeout=120, headers=BASE_HEADERS) as client:
            # HEAD к воркеру — чтобы узнать длину/тип (если отдаёт)
            head = await client.head(purl)
            clen = head.headers.get("content-length")
            ctype = head.headers.get("content-type", "video/mp4")

            # основной стрим (GET) с поддержкой Range
            req = client.build_request("GET", purl, headers=headers)
            upstream = await client.send(req, stream=True)

            async def gen():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()

        status = upstream.status_code
        resp = StreamingResponse(gen(), status_code=status, media_type=ctype)

        # Имя файла
        fn_ascii, fn_utf8 = safe_filename(title)
        resp.headers["Content-Disposition"] = (
            f'inline; filename="{fn_ascii}"; filename*=UTF-8\'\'{urlquote(fn_utf8)}'
        )

        # Пробрасываем важные заголовки
        for h in ["content-length", "content-range", "accept-ranges", "etag", "last-modified", "cache-control"]:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]

        # Если HEAD дал длину, а апстрим не дал — подставим
        if "content-length" not in resp.headers and clen and "range" not in request.headers:
            resp.headers["Content-Length"] = clen

        resp.headers.setdefault("Accept-Ranges", "bytes")
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        return resp

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
