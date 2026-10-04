import base64, hashlib, json, os, re, sqlite3, subprocess, tempfile, threading, time, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

# 앞 모델이 과부하(503)거나 무료 한도 초과(429)면 다음 모델로 (모델마다 한도가 따로)
MODELS = [os.environ.get("GEMINI_MODEL", "gemini-flash-latest"), "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite"]
COOKIES = os.environ.get("COOKIES_BROWSER")  # e.g. "chrome" or "safari", for Instagram login walls
TOKEN = os.environ.get("APP_TOKEN")  # 설정하면 모든 /api 요청에 X-Token 헤더 필요 (공개 서버용)
# 인스타 로그인 벽 우회용 쿠키(Netscape 형식). 우선순위: COOKIES_B64(base64 한 줄, 붙여넣기 사고에 안전) > COOKIES_TXT > Secret File
_secret = Path("/etc/secrets/cookies.txt")
COOKIES_ERR = None  # 설정이 잘못돼도 서버는 뜨고, 인스타 요청 때 이유를 알려준다


def load_cookies():
    global COOKIES_ERR
    b64 = os.environ.get("COOKIES_B64")
    if b64:
        try:
            b64 = "".join(b64.split()).strip("\"'")
            return base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode()
        except Exception:
            COOKIES_ERR = f"COOKIES_B64 값이 올바른 base64가 아닙니다 (현재 {len(b64)}자). 다시 복사해 붙여넣으세요."
            return None
    return os.environ.get("COOKIES_TXT") or (_secret.read_text() if _secret.exists() else None)


COOKIES_TXT = load_cookies()
DB_URL = os.environ.get("DATABASE_URL")  # 있으면 Postgres(클라우드), 없으면 로컬 SQLite 파일
DB_FILE = Path(__file__).parent / "recipes.db"
if os.environ.get("RENDER") and not DB_URL:  # Render 디스크는 재시작 때 지워지므로, DB 없이 뜨면 레시피가 조용히 사라진다
    raise SystemExit("DATABASE_URL 환경변수가 필요합니다 (Render Environment에 Postgres 연결 문자열 설정)")
HERE = Path(__file__).parent

client = genai.Client()  # reads GEMINI_API_KEY
app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1000)  # 목록 JSON(썸네일 포함) 전송량 감소


class Ingredient(BaseModel):
    name: str
    amount: str


class Step(BaseModel):
    text: str
    timestamp: str  # "mm:ss" in the video where this step is shown, "" if unknown
    ingredients: list[Ingredient]


class Section(BaseModel):
    title: str
    steps: list[Step]


class Recipe(BaseModel):
    title: str
    servings: str
    minutes: int
    ingredients: list[Ingredient]
    sections: list[Section]
    tips: list[str]


PROMPT = """첨부된 요리 콘텐츠(영상, 사진 게시물, 또는 글)를 종합해 한국어 레시피를 만들어라.
- 영상이면 화면·음성·자막을, 사진 게시물이면 사진 속 글자와 사진 순서를, 글이면 본문을 근거로 삼는다.
- 아래 캡션/본문에 재료·분량이 있으면 가장 우선 신뢰하고, 영상·사진과 다르면 영상·사진 기준으로 보정.
- 재료는 같은 것끼리 합치고 분량은 단위를 통일(g, ml, 큰술 등). 분량을 알 수 없으면 "적당량".
- 조리 순서는 섹션으로 나누고, 각 단계에 그 단계에 쓰는 재료를 붙여라. timestamp는 영상일 때만 해당 시점(mm:ss)을 쓰고, 사진·글이면 빈 문자열.
- 콘텐츠에 없는 내용은 지어내지 마라. 요리 레시피가 아니면 title에 "레시피 아님"이라고 쓰고 나머지는 비워라.

캡션/본문:
{caption}
"""


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True)


UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"


def http_get(u):
    return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30)


@lru_cache(maxsize=32)  # 한 번의 추출에서 같은 페이지를 여러 번(커버·게시자·종류 판별) 읽으므로 캐시
def embed_page(url: str) -> str:
    m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", url)
    if not m:
        raise ValueError("인스타 게시물 주소가 아님")
    return http_get(f"https://www.instagram.com/reel/{m[1]}/embed/").read().decode("utf8", "ignore")


def embed_fields(page: str, key: str):
    """임베드 페이지의 JSON(문자열 안에 2겹 이스케이프)에서 key의 모든 값."""
    return [json.loads('"' + json.loads('"' + m[1] + '"') + '"') for m in re.finditer(key + r'\\":\\"(.*?)\\"', page)]


def embed_field(page: str, key: str):
    f = embed_fields(page, key)
    return f[0] if f else None


def embed_caption(page: str) -> str:
    m = re.search(r'edge_media_to_caption\\":\{\\"edges\\":\[\{\\"node\\":\{\\"text\\":\\"(.*?)(?<!\\\\)\\"', page)
    return json.loads('"' + json.loads('"' + m[1] + '"') + '"') if m else ""


def fetch_embed(url: str, d: str):
    """yt-dlp가 막혔을 때: 인스타 공개 임베드 페이지에 들어있는 video_url을 직접 받는다 (ponytail: 페이지 구조가 바뀌면 깨짐)."""
    video_url = embed_field(embed_page(url), "video_url")
    if not video_url:
        raise ValueError("임베드 페이지에 영상 주소가 없음 (영상이 아니거나 접근 제한)")
    out = Path(d) / "v.mp4"
    with http_get(video_url) as r, open(out, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return out, ""


def fetch(url: str, d: str):
    base = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if COOKIES:
        base += ["--cookies-from-browser", COOKIES]
    if COOKIES_ERR:
        raise HTTPException(500, COOKIES_ERR)
    if COOKIES_TXT:
        lines = COOKIES_TXT.splitlines()
        if not any(l.count("\t") == 6 for l in lines):  # 탭 7칸 = 정상 쿠키 줄
            raise HTTPException(500, "쿠키 설정이 올바르지 않습니다 (Netscape 형식 쿠키 줄이 없음). COOKIES_B64를 다시 만드세요.")
        if not any("\tsessionid\t" in l for l in lines):
            raise HTTPException(500, "쿠키에 로그인 정보(sessionid)가 없습니다. 값이 잘렸거나 로그아웃 상태에서 추출했을 수 있습니다.")
        (Path(d) / "cookies.txt").write_text(COOKIES_TXT)
        base += ["--cookies", f"{d}/cookies.txt"]
    try:
        meta = json.loads(run(base + ["--dump-json", url]).stdout)
        run(base + ["-f", "bv*[height<=480]+ba/b[height<=480]/b", "--merge-output-format", "mp4", "-o", f"{d}/v.%(ext)s", url])
    except subprocess.CalledProcessError as e:
        errs = " ".join(l for l in e.stderr.splitlines() if l.startswith("ERROR"))  # 핵심 ERROR 줄만
        if "instagram.com" not in url:  # 임베드 대체 경로는 인스타 전용 — 다른 사이트 오류에 인스타 얘기를 섞지 않는다
            raise HTTPException(502, f"영상 다운로드 실패: {(errs or e.stderr.strip())[:300]}")
        try:
            return fetch_embed(url, d)
        except Exception as e2:
            raise HTTPException(502, f"영상 다운로드 실패: {(errs or e.stderr.strip())[:300]} | 임베드 대체 경로도 실패: {e2}")
    video = next(Path(d).glob("v.*"))
    return video, meta.get("description") or meta.get("title") or ""


def thumb_from(src: str, d: str, still: bool = False):
    """가운데를 정사각형으로 자른 168px JPEG를 data URI로. 실패하면 None (썸네일 때문에 레시피가 실패하면 안 됨)."""
    out = f"{d}/t.jpg"
    for seek in ([[]] if still else [["-ss", "1"], []]):  # 영상은 1초 지점(안 되면 첫 프레임), 이미지는 그대로
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *seek, "-i", src, "-frames:v", "1",
                            "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=168:168", "-q:v", "6", out],
                           check=True, capture_output=True, timeout=60)
            return "data:image/jpeg;base64," + base64.b64encode(Path(out).read_bytes()).decode()
        except Exception as e:
            print("thumb_from failed:", getattr(e, "stderr", b"")[-200:] or e, flush=True)
    return None


def youtube_thumb(url: str, d: str):
    try:
        vid = parse_qs(urlparse(url).query)["v"][0]
        path = f"{d}/yt.jpg"
        with urllib.request.urlopen(f"https://img.youtube.com/vi/{vid}/mqdefault.jpg", timeout=15) as r, open(path, "wb") as f:
            f.write(r.read())
        return thumb_from(path, d, still=True)
    except Exception as e:
        print("youtube_thumb failed:", e, flush=True)
        return None


def instagram_cover(url: str, d: str):
    """게시물 대표 이미지(작성자가 고른 커버). 영상 프레임보다 보기 좋고, 영상을 안 받아도 된다."""
    try:
        cover = embed_field(embed_page(url), "display_url")
        if not cover:
            return None
        path = f"{d}/cover.jpg"
        with http_get(cover) as r, open(path, "wb") as f:
            f.write(r.read())
        return thumb_from(path, d, still=True)
    except Exception as e:
        print("instagram_cover failed:", e, flush=True)
        return None


def instagram_author(url: str):
    """(@아이디, 프로필 주소). 임베드 페이지의 프로필 링크에서 읽는다 — 공동 게시물이면 첫 번째 게시자."""
    try:
        page = embed_page(url)
        m = re.search(r"user\?username=([\w.]+)", page)
        name = m[1] if m else embed_field(page, "username")
        return ("@" + name, f"https://www.instagram.com/{name}/") if name else (None, None)
    except Exception as e:
        print("instagram_author failed:", e, flush=True)
        return None, None


def youtube_author(url: str):
    """(채널 이름, 채널 주소) — oEmbed, 키 불필요."""
    try:
        with http_get("https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")) as r:
            d = json.load(r)
        return d.get("author_name"), d.get("author_url")
    except Exception as e:
        print("youtube_author failed:", e, flush=True)
        return None, None


def author_for(url: str):
    """(이름, 프로필 주소) — 못 구하면 (None, None)"""
    if re.match(r"https?://(www\.|m\.)?youtube\.com/", url):
        return youtube_author(url)
    if "instagram.com" in url:
        return instagram_author(url)
    u = urlparse(url)  # 일반 페이지: 사이트 도메인을 게시자로 (네트워크 호출 없음)
    return (u.netloc.removeprefix("www."), f"{u.scheme}://{u.netloc}/") if u.netloc else (None, None)


def thumb_for(url: str, d: str):
    """영상을 받지 않고 만들 수 있는 썸네일 (이미 저장된 레시피에 채워 넣을 때 사용)."""
    if re.match(r"https?://(www\.|m\.)?youtube\.com/", url):
        return youtube_thumb(url, d)
    return instagram_cover(url, d) if "instagram.com" in url else None


def instagram_kind(url: str):
    """'video' | 'image' | None(임베드를 못 읽음). 사진 게시물(여러 장 포함)은 영상 주소가 없다."""
    try:
        page = embed_page(url)
    except Exception:
        return None
    if embed_field(page, "video_url"):
        return "video"
    return "image" if embed_fields(page, "display_url") else None


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "svg", "form"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in self.SKIP

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.out.append(data.strip())


def fetch_webpage(url: str):
    """블로그/웹 페이지 -> (본문 텍스트, 대표 이미지 주소 또는 None). ponytail: 자바스크립트로 그리는 페이지는 본문이 비어 실패한다."""
    m = re.match(r"https?://blog\.naver\.com/([\w-]+)/(\d+)", url)
    if m:  # PC 네이버 블로그는 본문이 iframe 안이라 비어 보인다 -> 모바일 주소는 본문이 바로 들어 있다
        url = f"https://m.blog.naver.com/{m[1]}/{m[2]}"
    with http_get(url) as r:
        html = r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "ignore")
    t = _Text()
    t.feed(html)
    text = re.sub(r"\s+", " ", " ".join(t.out))[:20000]
    og = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html) \
        or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
    return text, urllib.parse.urljoin(url, og[1]) if og else None


def make_recipe(url: str):
    """(레시피 dict, 썸네일 data URI 또는 None, (게시자 이름, 프로필 주소))"""
    with tempfile.TemporaryDirectory() as d:
        if re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", url):
            # 유튜브는 다운로드 없이 Gemini가 URL로 직접 시청 (클라우드 IP 차단 회피)
            thumb = youtube_thumb(url, d)
            return json.loads(generate(types.Part(file_data=types.FileData(file_uri=url)), "").text), thumb, author_for(url)
        if "instagram.com" in url and instagram_kind(url) == "image":
            # 사진 게시물(여러 장 포함): 캡션 + 사진 전체를 Gemini에 넘긴다
            page = embed_page(url)
            parts = []
            for u in list(dict.fromkeys(embed_fields(page, "display_url")))[:10]:  # 중복 제거, 최대 10장
                with http_get(u) as r:
                    parts.append(types.Part.from_bytes(data=r.read(), mime_type=r.headers.get_content_type() or "image/jpeg"))
            return json.loads(generate(parts, embed_caption(page)).text), instagram_cover(url, d), author_for(url)
        try:
            video, caption = fetch(url, d)
        except HTTPException as e:
            if "instagram.com" in url:
                raise
            # 영상이 아니면 웹 페이지 본문으로 시도 (실패하면 영상 다운로드 오류도 함께 보여준다)
            try:
                text, img = fetch_webpage(url)
            except Exception as e2:
                raise HTTPException(502, f"{e.detail} | 웹 페이지 읽기도 실패: {e2}")
            if len(text) < 200:
                raise HTTPException(502, f"{e.detail} | 페이지에서 본문을 읽지 못했습니다 (로그인이 필요하거나 스크립트로 그리는 페이지일 수 있어요)")
            thumb = None
            if img:
                try:
                    with http_get(img) as r, open(f"{d}/og.jpg", "wb") as f:
                        f.write(r.read())
                    thumb = thumb_from(f"{d}/og.jpg", d, still=True)
                except Exception as e3:
                    print("og:image failed:", e3, flush=True)
            return json.loads(generate(None, text).text), thumb, author_for(url)
        thumb = instagram_cover(url, d) or thumb_from(str(video), d)  # 대표 이미지 우선, 없으면 영상 프레임
        f = client.files.upload(file=str(video))
        while f.state.name == "PROCESSING":
            time.sleep(2)
            f = client.files.get(name=f.name)
        if f.state.name != "ACTIVE":
            raise HTTPException(502, "Gemini 영상 처리 실패")
        try:
            r = generate(f, caption)
        finally:
            client.files.delete(name=f.name)
    return json.loads(r.text), thumb, author_for(url)


def generate(f, caption):
    """f: 영상 파일 / 이미지 Part 목록 / None(글만)"""
    media = [] if f is None else f if isinstance(f, list) else [f]
    err = None
    for attempt in range(3):
        for model in MODELS:
            try:
                return client.models.generate_content(
                    model=model,
                    contents=[*media, PROMPT.format(caption=caption)],
                    config={"response_mime_type": "application/json", "response_schema": Recipe},
                )
            except genai.errors.APIError as e:
                err = e
        time.sleep(3 * (attempt + 1))
    raise HTTPException(502, f"Gemini 오류 {err.code}: {err.message[:200]}")


jobs: dict = {}


_conn, _lock = None, threading.Lock()


def db(sql, args=()):
    """SQLite/Postgres 공용. SQL은 ? 자리표시자로 작성. Postgres는 연결을 재사용한다(질문마다 TLS 연결을 새로 맺으면 느림)."""
    global _conn
    if not DB_URL:
        con = sqlite3.connect(DB_FILE, isolation_level=None)
        try:
            cur = con.execute(sql, args)
            return cur.fetchall() if cur.description else []
        finally:
            con.close()
    import psycopg
    sql = sql.replace("?", "%s")
    with _lock:
        for attempt in (0, 1):
            try:
                if _conn is None or _conn.closed:
                    _conn = psycopg.connect(DB_URL, autocommit=True)
                cur = _conn.execute(sql, args)
                return cur.fetchall() if cur.description else []
            except (psycopg.OperationalError, psycopg.InterfaceError):  # Neon이 유휴 연결을 끊었을 때 한 번 재연결
                _conn = None
                if attempt:
                    raise


db("CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL)")
db("""CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
      data TEXT NOT NULL, folder_id TEXT, created TEXT NOT NULL)""")


for col in ("thumb", "ings", "author", "author_url"):
    try:
        db(f"ALTER TABLE recipes ADD COLUMN {col} TEXT")  # 이미 있으면 에러 -> 무시 (SQLite/Postgres 공용)
    except Exception:
        pass


def ings_of(data: dict) -> str:  # 검색용: 재료 이름만 이어 붙인 문자열
    return "|".join(i["name"] for i in data.get("ingredients", []))


for rid, data in db("SELECT id, data FROM recipes WHERE ings IS NULL"):  # 검색 기능 이전에 저장된 레시피 채우기
    db("UPDATE recipes SET ings = ? WHERE id = ?", (ings_of(json.loads(data)), rid))


def new_id():
    return uuid.uuid4().hex


def now():
    return datetime.now(timezone.utc).isoformat()


def norm(url):  # 같은 게시물이 추적 파라미터(igsh, si 등)만 달라 중복 저장되지 않게
    u = urlparse(url)
    vid = u.path.strip("/").split("/")[-1]
    if u.netloc == "youtu.be" or (u.netloc.endswith("youtube.com") and u.path.startswith("/shorts/")):
        return f"https://www.youtube.com/watch?v={vid}"  # 짧은 링크/쇼츠도 watch 형태로 통일
    if u.netloc.endswith("youtube.com"):
        query = urlencode({k: v for k, v in parse_qs(u.query).items() if k == "v"}, doseq=True)
    elif u.netloc.endswith("instagram.com"):
        query = ""
    else:
        query = u.query  # 블로그 등 일반 페이지는 글을 가리키는 값이 쿼리에 있는 경우가 많아 유지
    return urlunparse((u.scheme, u.netloc, u.path.rstrip("/") if query == "" else u.path, "", query, ""))


def check(token):
    if TOKEN and token != TOKEN:
        raise HTTPException(401, "접근 토큰이 올바르지 않습니다")


def by_url(url):
    r = db("SELECT id, data, folder_id, thumb, author, author_url FROM recipes WHERE url = ?", (url,))
    return {**json.loads(r[0][1]), "id": r[0][0], "folder_id": r[0][2], "thumb": r[0][3], "author": r[0][4], "author_url": r[0][5]} if r else None


def work(job_id: str, url: str, folder_id):
    try:
        data, thumb, (author, author_url) = make_recipe(url)
        if data["title"].strip() == "레시피 아님":  # 프롬프트가 요리와 무관한 콘텐츠에 쓰라고 한 표식 — 저장하지 않는다
            raise HTTPException(422, "요리 레시피가 아닌 콘텐츠로 보여 저장하지 않았습니다")
        data["url"] = url
        db("INSERT INTO recipes (id, url, title, data, folder_id, created, thumb, ings, author, author_url) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (url) DO NOTHING",
           (new_id(), url, data["title"], json.dumps(data, ensure_ascii=False), folder_id, now(), thumb, ings_of(data), author, author_url))
        jobs[job_id]["status"], jobs[job_id]["recipe"] = "done", by_url(url)
    except HTTPException as e:
        jobs[job_id].update(status="error", error=e.detail)
    except Exception as e:  # ponytail: 예상 못 한 오류도 화면에 보이게만 함
        jobs[job_id].update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/recipe")
def recipe(body: dict, x_token: str | None = Header(None)):
    check(x_token)
    m = re.search(r"https?://\S+", body.get("url") or "")  # 공유 텍스트에서 링크만 추출
    if not m:
        raise HTTPException(400, "링크를 찾을 수 없습니다")
    url = norm(m.group(0))
    if saved := by_url(url):
        return {"status": "done", "recipe": saved}
    job_id = new_id()
    jobs[job_id] = {"status": "pending", "url": url}
    threading.Thread(target=work, args=(job_id, url, body.get("folder_id")), daemon=True).start()
    return {"status": "pending", "id": job_id}


@app.get("/api/job/{job_id}")
def job(job_id: str, x_token: str | None = Header(None)):
    check(x_token)
    return jobs.get(job_id) or {"status": "error", "error": "작업을 찾을 수 없습니다 (서버가 재시작됐을 수 있음)"}


@app.delete("/api/job/{job_id}")
def dismiss(job_id: str, x_token: str | None = Header(None)):
    check(x_token)
    jobs.pop(job_id, None)
    return {}


@app.get("/api/library")
def library(x_token: str | None = Header(None)):
    check(x_token)
    return {
        "folders": [{"id": r[0], "name": r[1]} for r in db("SELECT id, name FROM folders ORDER BY name")],
        "recipes": [{"id": r[0], "title": r[1], "folder_id": r[2], "thumb": r[3], "ings": r[4], "author": r[5]}
                    for r in db("SELECT id, title, folder_id, thumb, ings, author FROM recipes ORDER BY created DESC")],
        "jobs": [{"id": k, **{x: v[x] for x in ("status", "url", "error") if x in v}}
                 for k, v in jobs.items() if v["status"] != "done"],
    }


@app.get("/api/recipes/{rid}")
def get_recipe(rid: str, x_token: str | None = Header(None)):
    check(x_token)
    r = db("SELECT id, data, folder_id, thumb, author, author_url FROM recipes WHERE id = ?", (rid,))
    if not r:
        raise HTTPException(404, "레시피를 찾을 수 없습니다")
    return {**json.loads(r[0][1]), "id": r[0][0], "folder_id": r[0][2], "thumb": r[0][3], "author": r[0][4], "author_url": r[0][5]}


@app.patch("/api/recipes/{rid}")
def move_recipe(rid: str, body: dict, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE recipes SET folder_id = ? WHERE id = ?", (body.get("folder_id") or None, rid))
    return {}


@app.delete("/api/recipes/{rid}")
def delete_recipe(rid: str, x_token: str | None = Header(None)):
    check(x_token)
    db("DELETE FROM recipes WHERE id = ?", (rid,))
    return {}


@app.post("/api/folders")
def add_folder(body: dict, x_token: str | None = Header(None)):
    check(x_token)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "폴더 이름이 필요합니다")
    fid = new_id()
    db("INSERT INTO folders (id, name, created) VALUES (?,?,?)", (fid, name, now()))
    return {"id": fid, "name": name}


@app.patch("/api/folders/{fid}")
def rename_folder(fid: str, body: dict, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE folders SET name = ? WHERE id = ?", ((body.get("name") or "").strip(), fid))
    return {}


@app.delete("/api/folders/{fid}")
def delete_folder(fid: str, x_token: str | None = Header(None)):
    check(x_token)
    db("UPDATE recipes SET folder_id = NULL WHERE folder_id = ?", (fid,))  # 레시피는 '분류 안 됨'으로 이동
    db("DELETE FROM folders WHERE id = ?", (fid,))
    return {}


def _build() -> str:  # 화면 파일 내용의 해시. 배포로 파일이 바뀌면 값이 달라진다
    h = hashlib.sha1()
    for n in ("index.html", "sw.js", "manifest.json", "icon.png"):
        h.update((HERE / n).read_bytes())
    return h.hexdigest()[:10]


BUILD = _build()


@app.get("/")
def index():
    # 화면에 자기 버전을 심어 보낸다. 화면이 /version과 비교해 낡았으면 스스로 새로고침한다.
    html = (HERE / "index.html").read_text(encoding="utf8").replace("__BUILD__", BUILD)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/version")
def version():
    return {"v": BUILD}


@app.get("/manifest.json")
def manifest():
    return FileResponse(HERE / "manifest.json")


@app.get("/icon.png")
def icon():
    return FileResponse(HERE / "icon.png")


def backfill():
    """썸네일/게시자가 비어 있는 기존 레시피를 서버 시작 때 채운다 (한 번에 최대 20개, 못 구하면 다음 시작 때 재시도).
    ponytail: 영구히 못 구하는 항목이 20개 넘게 쌓이면 뒤쪽이 밀린다 — 그때는 실패 표시 컬럼 추가."""
    for rid, url, thumb, author, author_url in db("SELECT id, url, thumb, author, author_url FROM recipes WHERE thumb IS NULL OR author IS NULL OR author_url IS NULL ORDER BY created DESC LIMIT 20"):
        if thumb is None:
            with tempfile.TemporaryDirectory() as d:
                thumb = thumb_for(url, d)
            if thumb:
                db("UPDATE recipes SET thumb = ? WHERE id = ?", (thumb, rid))
        if author is None or author_url is None:
            name, link = author_for(url)
            if name and link:
                db("UPDATE recipes SET author = ?, author_url = ? WHERE id = ?", (name, link, rid))


threading.Thread(target=backfill, daemon=True).start()


@app.get("/health")
def health():  # 외부 핑(UptimeRobot)용: DB를 건드리지 않는다
    return {"ok": True}


@app.get("/sw.js")
def sw():
    return FileResponse(HERE / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})
