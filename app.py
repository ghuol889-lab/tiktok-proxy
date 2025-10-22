# app.py — FastAPI + yt-dlp
# /api?url=... → JSON с прямым .mp4
# /dl?url=...  → корректный поток (200/206 + Range) и нормальное имя файла

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import yt_dlp
import httpx
import re
from urllib.parse import quote as urlquote

app = FastAPI()

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124 Safari/537.36"
)

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
    """
    Возвращаем (video_url, title, headers) — headers берём из yt-dlp (там cookies и т.п.)
    и дополняем своим UA/Referer.
    """
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": UA, "Referer": "https://www.tiktok.com/"},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    vurl = pick_format(info)
    title = info.get("title")
    hdrs = (info.get("http_headers") or {}).copy()
    hdrs.setdefault("User-Agent", UA)
    hdrs.setdefault("Referer", "https://www.tiktok.com/")
    # Небольшой бонус — некоторые CDN любят Origin/Accept
    hdrs.setdefault("Origin", "https://www.tiktok.com")
    hdrs.setdefault("Accept", "*/*")
    return vurl, title, hdrs

def make_filename(title: str | None) -> tuple[str, str]:
    base = (title or "video").strip()
    base = re.sub(r'[\\/*?:"<>|]+', "_", base)
    base = re.sub(r"\s+", " ", base).strip()[:80] or "video"
    fn_utf8 = f"{base}.mp4"
    fn_ascii = fn_utf8.encode("ascii", "ignore").decode("ascii") or "video.mp4"
    return fn_ascii, fn_utf8

@app.get("/")
def root():
    return {"ok": True}

@app.get("/api")
def api(url: str):
    try:
        vurl, title, _ = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)
        return {"ok": True, "video_url": vurl, "title": title}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@app.get("/dl")
async def dl(request: Request, url: str):
    """
    Стримим видео и корректно обслуживаем Range:
    - Берём статус/заголовки у TikTok ДО ответа
    - Прокидываем все важные заголовки (включая cookies от yt-dlp)
    """
    try:
        vurl, title, base_headers = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)

        # Заголовки для запроса на CDN
        req_headers = base_headers.copy()
        if "range" in request.headers:
            req_headers["Range"] = request.headers["range"]

        client = httpx.AsyncClient(follow_redirects=True, timeout=120)
        req = client.build_request("GET", vurl, headers=req_headers)
        upstream = await client.send(req, stream=True)

        status = upstream.status_code  # 200/206/403...
        ctype  = upstream.headers.get("content-type", "video/mp4")

        async def generate():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        resp = StreamingResponse(generate(), status_code=status, media_type=ctype)

        # Красивое имя файла
        fn_ascii, fn_utf8 = make_filename(title)
        resp.headers["Content-Disposition"] = (
            f'inline; filename="{fn_ascii}"; filename*=UTF-8\'\'{urlquote(fn_utf8)}'
        )

        # Пробрасываем важные заголовки
        for h in ["content-length", "content-range", "accept-ranges", "etag", "last-modified"]:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]
        resp.headers.setdefault("Accept-Ranges", "bytes")
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")
        return resp

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
