# app.py — FastAPI + yt-dlp
# /api?url=... → JSON с прямым .mp4
# /dl?url=...  → корректный поток (200/206 + Range),
#                прогрев cookies + HEAD для Content-Length,
#                красивое имя файла

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

BASE_CLIENT_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.tiktok.com/",
    "Origin": "https://www.tiktok.com",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9,ru-RU;q=0.8,ru;q=0.7",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "TE": "trailers",
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
        "http_headers": BASE_CLIENT_HEADERS.copy(),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    vurl = pick_format(info)
    title = info.get("title")
    hdrs = (info.get("http_headers") or {}).copy()
    for k, v in BASE_CLIENT_HEADERS.items():
        hdrs.setdefault(k, v)
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
    1) yt-dlp → vurl, заголовки
    2) httpx.AsyncClient одной сессией:
       2.1) GET к странице ролика (прогрев cookies)
       2.2) HEAD к vurl → Content-Length/Type
       2.3) GET stream к vurl (пробрасываем Range/headers)
    """
    try:
        vurl, title, base_headers = extract(url)
        if not vurl:
            return JSONResponse({"ok": False, "error": "no_video"}, status_code=404)

        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=120,
            headers=base_headers,
        )

        # Прогрев страницы ролика — получить куки
        try:
            await client.get(url)
        except Exception:
            pass

        # HEAD к mp4 — чтобы узнать длину и тип
        head_len = None
        head_type = None
        head_accept_ranges = None
        try:
            hr = await client.head(vurl, headers=base_headers)
            if 200 <= hr.status_code < 400:
                head_len = hr.headers.get("content-length")
                head_type = hr.headers.get("content-type")
                head_accept_ranges = hr.headers.get("accept-ranges")
        except Exception:
            pass

        # Собираем заголовки запроса к mp4
        req_headers = base_headers.copy()
        if "range" in request.headers:
            req_headers["Range"] = request.headers["range"]

        req = client.build_request("GET", vurl, headers=req_headers)
        upstream = await client.send(req, stream=True)

        status = upstream.status_code
        ctype  = upstream.headers.get("content-type", head_type or "video/mp4")

        async def generate():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        resp = StreamingResponse(generate(), status_code=status, media_type=ctype)

        # Имя файла
        fn_ascii, fn_utf8 = make_filename(title)
        resp.headers["Content-Disposition"] = (
            f'inline; filename="{fn_ascii}"; filename*=UTF-8\'\'{urlquote(fn_utf8)}'
        )

        # Пробрасываем заголовки от апстрима
        for h in ["content-length", "content-range", "accept-ranges", "etag", "last-modified"]:
            if h in upstream.headers:
                resp.headers[h] = upstream.headers[h]

        # Если это не Range-запрос и у нас есть длина из HEAD — выставим её:
        if "content-length" not in resp.headers and head_len and "range" not in request.headers:
            resp.headers["Content-Length"] = head_len

        # Запасные значения
        resp.headers.setdefault("Accept-Ranges", head_accept_ranges or "bytes")
        resp.headers.setdefault("Cache-Control", "public, max-age=86400")

        return resp

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
