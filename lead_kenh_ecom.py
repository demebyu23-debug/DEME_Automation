import os
import io
import csv
import base64
import sys
import time
import re
import datetime
import requests


def _load_env_file(path: str = ".env") -> None:
    """Chạy local: đọc file .env (cùng thư mục script hoặc thư mục đang đứng). Biến đã có sẵn (GitHub Secrets) không bị ghi đè."""
    for folder in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        fp = os.path.join(folder, path)
        if not os.path.isfile(fp):
            continue
        with open(fp, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k and not os.environ.get(k):
                    os.environ[k] = v
        print(f"📄 Đã đọc biến từ {fp}")
        return


_load_env_file()

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
CSV_FILE = "pages_config_full.csv"
DELAY_BETWEEN_CALLS = 1.0  # nghỉ giữa mỗi lần gọi API để tránh rate-limit
DRY_RUN = False            # True = chỉ in ra, không ghi vào Lark

# Mỗi lần chạy mặc định quét lại SO_NGAY_QUET_LAI ngày gần nhất (kết thúc NGÀY CHẠY, gồm cả hôm nay) để bắt số liệu thay đổi
# (ví dụ ID đơn được ghi chú muộn); chỉ ghi lại vào Lark khi số liệu KHÁC với giá trị đang có.
# Số liệu của ngày chạy là số tạm tính đến thời điểm chạy, các lần chạy sau sẽ tự cập nhật lại.
SO_NGAY_QUET_LAI = 3

# Chạy bù: đặt RUN_DATE_FROM và RUN_DATE_TO (dd/mm/yyyy). Chỉ đặt FROM = chạy từ ngày đó đến NGÀY HIỆN TẠI (gồm hôm nay).
# Để trống cả hai = quét lại SO_NGAY_QUET_LAI ngày gần nhất kết thúc ở ngày chạy (lịch hằng ngày).
RUN_DATE_FROM = os.getenv("RUN_DATE_FROM", "").strip()
RUN_DATE_TO   = os.getenv("RUN_DATE_TO", "").strip()

# ── PANCAKE ─────────────────────────────────────────────────────
# Public API: /pages/{page_id}/statistics/pages, dùng page_access_token riêng từng trang (cột access_token trong CSV)
BASE_URL = "https://pages.fm/api/public_api/v1"

# Page ID của kênh Zalo / Deme Uniform (token lấy trong secret PAGES_CONFIG_CSV_B64 theo page_id này)
ID_ZALO_DEME_UNIFORM = "pzl_759480667788997165"
PAGES_CONFIG_CSV_B64 = os.getenv("PAGES_CONFIG_CSV_B64", "").strip()

# ── LEAD ZALO TIẾP NHẬN THEO KÊNH ───────────────────────────────
# Lead zalo tiếp nhận của 1 kênh = số hội thoại Zalo / Deme Uniform TẠO MỚI trong ngày thuộc kênh đó:
#   hội thoại có ghi chú là ID đơn -> kênh "<nguồn đơn POS> / <page đơn>"; không có ID đơn -> "Không xác định".
POS_SHOP_ID = "1890165155"
# secret PANCAKE_API_KEY_1890165155 (API key của shop POS); nếu không có thì dùng biến POS_API_KEY
POS_API_KEY = (os.getenv(f"PANCAKE_API_KEY_{POS_SHOP_ID}", "") or os.getenv("POS_API_KEY", "")).strip()
POS_URL = f"https://pos.pages.fm/api/v1/shops/{POS_SHOP_ID}/orders"
API_V2 = "https://pages.fm/api/public_api/v2/pages"
REGEX_ID_DON = r"^\d{5,6}$"                                  # ghi chú khớp mẫu này = ID đơn hàng
MAX_LUOT = 200                                              # tối đa số lần gọi danh sách hội thoại (60 hội thoại / lần)
KENH_KHONG_XD = "Không xác định"
KENH_ALIAS = {"tiktok": "Tiktokshop"}                       # đổi tên nguồn POS cho khớp cột Kênh trên Lark
NGUON_FIELDS = ["order_sources_name", "order_source_name", "source_name", "order_sources", "order_source"]

# Các dòng cần có mỗi ngày trên Lark, đúng thứ tự hiển thị.
# Tên kênh = giá trị cột "Kênh" trên Lark. (platform, channel) = để tìm page trong CSV.
# None = không lấy từ Pancake, mặc định điền 0.
DAILY_ROWS: list[tuple[str, tuple[str, str] | None]] = [
    ("Không xác định",                          ("id", ID_ZALO_DEME_UNIFORM)),   # = Lead của Zalo / Deme Uniform
    ("Shopee / Deme Uniform",                   ("shopee", "Deme Uniform")),
    ("Shopee / Đồng Phục In Theo Yêu Cầu DEME", ("shopee", "Đồng Phục In Theo Yêu Cầu DEME")),
    ("Tiktokshop / Deme Đồng Phục",             ("tiktokshop", "Deme Đồng Phục")),
]

# ── LARK BASE ───────────────────────────────────────────────────
# Đọc từ biến môi trường (GitHub Secrets) — KHÔNG hardcode trong code
# Cần 3 secret: LARK_APP_ID, LARK_APP_SECRET, LARK_BASE_APP_TOKEN
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_BASE_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN", "").strip()
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID_LEAD", "").strip() or "tbl9EfjuWYr0NCRn"  # bảng Lead Ecom
LARK_URL = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"

FIELD_DATE = "Ngày"       # cột Ngày (Date)
FIELD_CHANNEL = "Kênh"    # cột Kênh (Single select)
FIELD_LEAD = "Lead"       # cột Lead (Number)
FIELD_ZALO_TN = "Lead zalo tiếp nhận"   # cột Lead zalo tiếp nhận (Number) - script điền
# (cột "Lead zalo chuyển đổi" là công thức trên Lark, script không ghi)

SESSION = requests.Session()


# ── PANCAKE HELPERS ─────────────────────────────────────────────
def _read_csv_text(path: str) -> str:
    """Ưu tiên secret PAGES_CONFIG_CSV_B64 (base64 của file CSV); không có thì đọc file CSV_FILE."""
    if PAGES_CONFIG_CSV_B64:
        try:
            return base64.b64decode(PAGES_CONFIG_CSV_B64).decode("utf-8-sig")
        except Exception as e:
            raise RuntimeError(f"PAGES_CONFIG_CSV_B64 không giải mã được base64/UTF-8: {e}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return f.read()


def load_tokens(path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """{(platform, channel) viết thường: (page_id, access_token)}; thêm {("id", page_id): (page_id, token)}"""
    result = {}
    for row in csv.DictReader(io.StringIO(_read_csv_text(path))):
        page_id = (row.get("page_id") or "").strip()
        token = (row.get("access_token") or "").strip()
        key = ((row.get("platform") or "").strip().lower(), (row.get("channel") or "").strip().lower())
        result[key] = (page_id, token)
        if page_id:
            result[("id", page_id.lower())] = (page_id, token)
    return result


def day_range_ts(d: datetime.date) -> tuple[int, int]:
    start = datetime.datetime.combine(d, datetime.time(0, 0, 0), tzinfo=TZ_VN)
    end = start + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def _find_record_list(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            return data["data"]
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and (
                "new_customer_count" in v[0] or "hour" in v[0]
            ):
                return v
    return []


def fetch_new_customers(page_id: str, token: str, d: datetime.date, retries=3) -> int | None:
    """Tổng new_customer_count trong ngày d. Trả về None nếu lỗi."""
    since, until = day_range_ts(d)
    url = f"{BASE_URL}/pages/{page_id}/statistics/pages"
    params = {"page_access_token": token, "since": since, "until": until}

    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"   ⚠️ Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
            continue

        if res.status_code == 429:
            time.sleep(10)
            continue
        if res.status_code >= 500:
            print(f"   ⚠️ HTTP {res.status_code}, thử lại...")
            time.sleep(5 * (attempt + 1))
            continue
        if res.status_code == 401:
            print(f"   ❌ 401 Unauthorized — page_access_token sai/hết quyền.")
            return None
        if res.status_code != 200:
            print(f"   ❌ HTTP {res.status_code}: {res.text[:150]}")
            return None

        data = res.json()
        if isinstance(data, dict) and data.get("success") is False:
            print(f"   ❌ Pancake báo lỗi: {data.get('message')}")
            return None
        return sum(int(r.get("new_customer_count", 0) or 0) for r in _find_record_list(data))
    return None


# ── LEAD ZALO TIẾP NHẬN THEO KÊNH ───────────────────────────────
def to_dt(s):
    """Thời gian Pancake (chuỗi ISO không múi giờ = UTC) -> giờ VN."""
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).astimezone(TZ_VN)
    except Exception:
        return None


def http_get(url: str, params: dict, retries=3):
    """(HTTP status, json/text). Thử lại khi lỗi mạng / 429 / 5xx. status 0 = không kết nối được."""
    last = (0, "không kết nối được")
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
            continue
        if res.status_code == 200:
            try:
                return 200, res.json()
            except ValueError:
                return 200, res.text[:200]
        if res.status_code in (400, 401, 403, 404):
            return res.status_code, res.text[:200]
        last = (res.status_code, res.text[:200])
        time.sleep(3 * (attempt + 1))
    return last


def id_don_trong_ghi_chu(c: dict) -> str:
    """ID đơn trong ghi chú được điền SỚM NHẤT của hội thoại (ghi chú chưa xóa, khớp REGEX_ID_DON); không có -> ''."""
    ung_vien = []
    for idx, n in enumerate((c.get("page_customer") or {}).get("notes") or []):
        if not isinstance(n, dict) or n.get("removed_at"):
            continue
        msg = str(n.get("message") or "").strip()
        if re.match(REGEX_ID_DON, msg):
            ung_vien.append((to_dt(n.get("created_at") or n.get("inserted_at")), idx, msg))
    if not ung_vien:
        return ""
    co_gio = [u for u in ung_vien if u[0]]
    return (min(co_gio, key=lambda u: (u[0], u[1])) if co_gio else ung_vien[0])[2]


def quet_zalo_tao_moi(page_id: str, token: str, days: list[datetime.date]):
    """Quét 1 lần hội thoại Zalo tạo mới trong các ngày `days`.
    Trả về ({ngày: [ID đơn hoặc '' cho mỗi hội thoại]}, đủ_dữ_liệu: bool)."""
    ket_qua = {d: [] for d in days}
    ngay_bd = datetime.datetime.combine(min(days), datetime.time(0, 0, 0), tzinfo=TZ_VN)
    last_id, xong = None, False
    for _ in range(MAX_LUOT):
        params = {"page_access_token": token}
        if last_id:
            params["last_conversation_id"] = last_id
        st, data = http_get(f"{API_V2}/{page_id}/conversations", params)
        if st != 200 or not isinstance(data, dict):
            print(f"   ❌ Lỗi lấy danh sách hội thoại Zalo: HTTP {st} {str(data)[:120]}")
            return ket_qua, False
        convs = data.get("conversations") or []
        if not convs:
            return ket_qua, True
        for c in convs:
            cn = to_dt(c.get("updated_at"))
            if cn and cn < ngay_bd:          # hội thoại tạo trong khoảng luôn có updated_at >= ngày bắt đầu
                xong = True
                continue
            tao = to_dt(c.get("inserted_at"))
            if tao and tao.date() in ket_qua:
                ket_qua[tao.date()].append(id_don_trong_ghi_chu(c))
        if xong:
            return ket_qua, True
        last_id = convs[-1].get("id")
        time.sleep(0.1)
    print(f"   ❌ Chạm giới hạn MAX_LUOT={MAX_LUOT} mà chưa quét hết hội thoại")
    return ket_qua, False


def _khop_don(o, id_don: str) -> bool:
    return isinstance(o, dict) and id_don in (str(o.get("id")), str(o.get("system_id")))


def _tim_don(du_lieu, id_don: str):
    if _khop_don(du_lieu, id_don):
        return du_lieu
    if isinstance(du_lieu, dict):
        for k in ("data", "order", "orders", "items"):
            v = du_lieu.get(k)
            if _khop_don(v, id_don):
                return v
            if isinstance(v, dict):
                r = _tim_don(v, id_don)
                if r:
                    return r
            if isinstance(v, list):
                for o in v:
                    if _khop_don(o, id_don):
                        return o
    if isinstance(du_lieu, list):
        for o in du_lieu:
            if _khop_don(o, id_don):
                return o
    return None


def kenh_cua_don(id_don: str):
    """Trả về (kênh, lỗi_tạm_thời). Không tìm thấy đơn / đơn không có nguồn -> Không xác định.
    lỗi_tạm_thời=True khi lỗi mạng / key sai / bị chặn (không chắc đơn có tồn tại) -> không nên ghi số liệu."""
    st, data = http_get(f"{POS_URL}/{id_don}", {"api_key": POS_API_KEY})
    o = _tim_don(data, id_don) if st == 200 else None
    st2 = None
    if o is None and st not in (401, 403):
        st2, data2 = http_get(POS_URL, {"api_key": POS_API_KEY, "search": id_don, "page_size": 20})   # dự phòng
        o = _tim_don(data2, id_don) if st2 == 200 else None
    if o is None:
        tam_thoi = st in (0, 401, 403) or st2 == 0 or (st2 is not None and st2 >= 500) or st >= 500
        return KENH_KHONG_XD, tam_thoi
    nguon = ""
    for k in NGUON_FIELDS:
        v = o.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title") or ""
        if v not in (None, "", -1):
            nguon = str(v)
            break
    p = o.get("page")
    page = str(p["name"]) if isinstance(p, dict) and p.get("name") else str(o.get("page_name") or "")
    if not nguon or not page:
        return KENH_KHONG_XD, False
    return f"{KENH_ALIAS.get(nguon.strip().lower(), nguon)} / {page}", False


def tinh_lead_zalo_tiep_nhan(page_id: str, token: str, days: list[datetime.date]) -> dict:
    """{ngày: {tên kênh viết thường: số lead}} cho các ngày tính được đầy đủ. Ngày thiếu dữ liệu / lỗi POS thì bỏ qua."""
    if not POS_API_KEY:
        print("   ❌ Thiếu PANCAKE_API_KEY_1890165155 - bỏ qua Lead zalo tiếp nhận (thêm secret này vào env của workflow / file .env)")
        return {}
    theo_ngay, ok = quet_zalo_tao_moi(page_id, token, days)
    if not ok:
        return {}
    cache, ket_qua = {}, {}
    for d in days:
        ds = theo_ngay[d]
        dem, loi = {}, False
        for id_don in ds:
            if not id_don:
                k = KENH_KHONG_XD
            else:
                if id_don not in cache:
                    cache[id_don] = kenh_cua_don(id_don)
                k, tam_thoi = cache[id_don]
                loi = loi or tam_thoi
            dem[k.lower()] = dem.get(k.lower(), 0) + 1
        if loi:
            print(f"   ❌ {d:%d/%m/%Y}: lỗi khi tra đơn POS (mạng / API key) - KHÔNG cập nhật Lead zalo tiếp nhận ngày này")
            continue
        ket_qua[d] = dem
        print(f"   📊 {d:%d/%m/%Y}: {len(ds)} hội thoại Zalo tạo mới, {sum(1 for x in ds if x)} có ID đơn")
    return ket_qua


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    res = SESSION.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30,
    ).json()
    if res.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {res}")
    return res["tenant_access_token"]


def _text(v) -> str:
    """Lấy chữ từ giá trị ô Lark (text, single select, hoặc list)."""
    if isinstance(v, list):
        return " ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in v).strip()
    if isinstance(v, dict):
        return str(v.get("text", "")).strip()
    return str(v or "").strip()


def build_index(token: str) -> dict[str, dict[str, str]]:
    """Tải toàn bộ record. Trả về {dd/mm/yyyy: {tên kênh viết thường: {"id": record_id, "fields": {...}}}}."""
    headers = {"Authorization": f"Bearer {token}"}
    index, page_token, total = {}, None, 0
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        res = SESSION.get(LARK_URL, headers=headers, params=params, timeout=30)
        try:
            data = res.json()
        except ValueError:
            raise RuntimeError(f"Lark trả HTTP {res.status_code}: {res.text[:200]}")
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi: {data} → kiểm tra base token / table ID / quyền của app.")

        for item in data["data"].get("items") or []:
            total += 1
            fields = item.get("fields", {})
            raw_date = fields.get(FIELD_DATE)
            if isinstance(raw_date, (int, float)):
                d_str = datetime.datetime.fromtimestamp(raw_date / 1000, tz=TZ_VN).strftime("%d/%m/%Y")
            else:
                d_str = _text(raw_date)
            kenh = _text(fields.get(FIELD_CHANNEL)).lower()
            if d_str and kenh:
                index.setdefault(d_str, {})[kenh] = {"id": item["record_id"], "fields": fields}

        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    print(f"✅ Đã tải {total} record từ bảng Lark.\n")
    return index


def lark_batch(token: str, action: str, records: list[dict]) -> int:
    """action = 'batch_create' hoặc 'batch_update'. Trả về số dòng ghi thành công."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    done = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        res = SESSION.post(f"{LARK_URL}/{action}", headers=headers, json={"records": chunk}, timeout=30).json()
        if res.get("code") == 0:
            done += len(chunk)
        else:
            print(f"   ❌ Lark {action} lỗi: code={res.get('code')} msg={res.get('msg')}")
    return done


# ── MAIN ──────────────────────────────────────────────────────
def parse_date(x: str) -> datetime.date:
    return datetime.datetime.strptime(x, "%d/%m/%Y").date()


def get_days() -> list[datetime.date]:
    today = datetime.datetime.now(TZ_VN).date()          # ngày chạy (giờ VN)
    if not RUN_DATE_FROM and not RUN_DATE_TO:
        return [today - datetime.timedelta(days=i) for i in range(SO_NGAY_QUET_LAI - 1, -1, -1)]
    start = parse_date(RUN_DATE_FROM) if RUN_DATE_FROM else parse_date(RUN_DATE_TO)
    end = parse_date(RUN_DATE_TO) if RUN_DATE_TO else today
    if start > end:
        print(f"🛑 Ngày bắt đầu {start:%d/%m/%Y} sau ngày kết thúc {end:%d/%m/%Y}.")
        sys.exit(1)
    return [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]


def main():
    if not LARK_APP_ID or not LARK_APP_SECRET:
        print("🛑 Thiếu LARK_APP_ID / LARK_APP_SECRET trong biến môi trường.")
        sys.exit(1)
    if not LARK_BASE_APP_TOKEN:
        print("🛑 Thiếu LARK_BASE_APP_TOKEN trong biến môi trường (kiểm tra secret và phần env trong file yml).")
        sys.exit(1)

    days = get_days()
    print(f"🚀 Chạy cho {len(days)} ngày: {days[0]:%d/%m/%Y} → {days[-1]:%d/%m/%Y} (giờ VN)\n")

    tokens = load_tokens(CSV_FILE)
    lark_token = lark_get_tenant_token()
    index = build_index(lark_token)

    to_create, to_update = [], []
    fail = 0

    # Lead zalo tiếp nhận theo kênh: quét 1 lần cho tất cả các ngày
    zalo_id, zalo_token = tokens.get(("id", ID_ZALO_DEME_UNIFORM.lower()), ("", ""))
    if zalo_token:
        print("🔎 Quét hội thoại Zalo / Deme Uniform để tính Lead zalo tiếp nhận theo kênh ...")
        lead_zalo = tinh_lead_zalo_tiep_nhan(zalo_id, zalo_token, days)
        fail += sum(1 for d in days if d not in lead_zalo)          # ngày không tính được -> run báo lỗi
        print()
    else:
        print("   ⚠️ Không có token của Zalo / Deme Uniform trong CSV - bỏ qua Lead zalo tiếp nhận.\n")
        lead_zalo, fail = {}, fail + 1

    for d in days:
        d_str = d.strftime("%d/%m/%Y")
        day_ms = day_range_ts(d)[0] * 1000   # giá trị cột Ngày (timestamp ms, 0h giờ VN)
        day_index = index.get(d_str, {})
        print(f"📅 {d_str} — Lark đã có {len(day_index)} dòng")

        for k, v in (lead_zalo.get(d) or {}).items():            # kênh lạ (không có dòng trong DAILY_ROWS) -> báo
            if k not in {lb.lower() for lb, _ in DAILY_ROWS}:
                print(f"   ⚠️ Kênh '{k}' có {v} lead Zalo nhưng không có dòng tương ứng trong DAILY_ROWS - chưa ghi")

        for label, source in DAILY_ROWS:
            if source is None:
                lead = 0
            else:
                page_id, page_token = tokens.get((source[0].lower(), source[1].lower()), ("", ""))
                if not page_id or not page_token:
                    print(f"   ⚠️ [{label}] Không có page_id/access_token trong CSV — để trống Lead.")
                    lead = None
                else:
                    lead = fetch_new_customers(page_id, page_token, d)
                    time.sleep(DELAY_BETWEEN_CALLS)
                if lead is None:
                    fail += 1

            print(f"   [{label}] Lead = {'(trống)' if lead is None else lead}")
            lead_field = {} if lead is None else {FIELD_LEAD: int(lead)}
            if d in lead_zalo:
                lead_field[FIELD_ZALO_TN] = int(lead_zalo[d].get(label.lower(), 0))
                print(f"   [{label}] {FIELD_ZALO_TN} = {lead_field[FIELD_ZALO_TN]}")

            rec = day_index.get(label.lower())
            if rec:
                cu = rec["fields"]
                doi = {}
                for fld, moi in lead_field.items():
                    hien_tai = cu.get(fld)
                    try:
                        khac = hien_tai is None or float(hien_tai) != float(moi)
                    except (TypeError, ValueError):
                        khac = True
                    if khac:
                        doi[fld] = moi
                        print(f"   ✏️ [{label}] {fld}: {hien_tai if hien_tai is not None else '(trống)'} → {moi}")
                if doi:
                    to_update.append({"record_id": rec["id"], "fields": doi})
            else:
                to_create.append({"fields": {FIELD_DATE: day_ms, FIELD_CHANNEL: label, **lead_field}})

    print(f"\n📝 Cần tạo mới {len(to_create)} dòng, cập nhật {len(to_update)} dòng.")
    if DRY_RUN:
        print("🛑 DRY_RUN=True — chưa ghi gì vào Lark.")
        return

    created = lark_batch(lark_token, "batch_create", to_create) if to_create else 0
    updated = lark_batch(lark_token, "batch_update", to_update) if to_update else 0
    fail += (len(to_create) - created) + (len(to_update) - updated)

    print(f"\n🏁 Xong | 🆕 tạo {created} | ✅ cập nhật {updated} | ❌ {fail} thất bại")
    if fail > 0:
        sys.exit(1)  # để GitHub Actions đánh dấu run này là failed


if __name__ == "__main__":
    main()