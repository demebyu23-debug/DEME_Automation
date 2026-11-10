"""
ĐỒNG BỘ PANCAKE -> LARK (bảng "Theo dõi chia lead theo sales")

Quét các kênh trong pages_config_full.csv (có token), PHÂN LOẠI kênh Zalo cá nhân của sale
(nhận diện theo PERSONAL_ZALO_PAGE_IDS): kênh Zalo cá nhân chỉ lấy mục 1 -> ghi 2 cột "LEAD Zalo cá nhân",
"KH cũ zalo cá nhân". Kênh chung lấy 2 loại dữ liệu:
  1. Tương tác khách hôm nay (00:00 -> lúc chạy): KH mới (LEAD) / KH cũ
     -> ghi 2 cột: LEAD kênh chung, KH cũ kênh chung
  2. Hội thoại chưa đọc VÀ tin cuối do khách gửi, trong 3 ngày gần nhất, theo sale được phân công
     -> ghi cột "Số hội thoại chưa phản hồi" cho tất cả sale có trong bảng
Khớp tên nhân viên Pancake với cột "Tên TK POS".

LƯU DỮ LIỆU THEO NGÀY (mới):
  - Mỗi lần chạy, đọc bảng "Đăng ký ca" để lấy danh sách người có ca làm HÔM NAY (bỏ OFF / NP).
  - Với mỗi người chưa có bản ghi hôm nay trong bảng theo dõi -> tạo bản ghi mới, điền cột "Sales".
    (chạy lần đầu của ngày sẽ tạo đủ, các lần sau không tạo trùng; ai đăng ký muộn sẽ được bổ sung)
  - Chỉ cập nhật số liệu cho các bản ghi của HÔM NAY. Bản ghi các ngày trước giữ nguyên.
  - Ngày của bản ghi lấy theo thời điểm tạo (cột "Ngày" kiểu Created time trong Lark).
"""
import os
import re
import time
import datetime
import unicodedata
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── CẤU HÌNH ────────────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
BASE_V1 = "https://pages.fm/api/public_api/v1"
BASE_V2 = "https://pages.fm/api/public_api/v2"
CSV_FILE = "pages_config_full.csv"

MAX_WORKERS = 5
DRY_RUN = False  # True = chỉ in log, không ghi / không tạo bản ghi trên Lark

# Để trống [] = lấy TẤT CẢ kênh trong CSV (trừ Zalo cá nhân).
CHANNEL_WHITELIST: list[tuple[str, str]] = []

# Các kênh Zalo CÁ NHÂN -- nhận diện theo page_id; chỉ lấy LEAD / KH cũ (ghi 2 cột Zalo cá nhân), KHÔNG đếm hội thoại chưa đọc.
# Danh sách lưu trong secret PERSONAL_ZALO_PAGE_IDS, các ID cách nhau bằng dấu phẩy.
# Vẫn cần secret này: thiếu thì script không biết kênh nào là Zalo cá nhân để bỏ qua.
PERSONAL_ZALO_PAGE_IDS: set[str] = {
    x.strip() for x in os.getenv("PERSONAL_ZALO_PAGE_IDS", "").split(",") if x.strip()
}

# Hội thoại chưa đọc
SO_GIO_CHUA_DOC = 72      # quét hội thoại cập nhật trong N tiếng gần nhất (72 = 3 ngày)
MAX_LUOT_HOI_THOAI = 30   # tối đa số lần gọi API hội thoại / kênh (60 hội thoại / lần)
LOC_THEO_CA = False       # False = điền cho TẤT CẢ sale có trong bảng; True = chỉ sale đang trong ca
XOA_NGOAI_CA = False      # True = sale ngoài ca bị xóa trống ô "Số hội thoại chưa phản hồi"
# Repo public: log GitHub Actions ai cũng xem được -> mặc định KHÔNG in số liệu theo tên sale.
# Muốn xem chi tiết khi dò lỗi: đặt biến LOG_THEO_SALE=1 (hoặc chạy trên máy).
LOG_THEO_SALE = os.getenv("LOG_THEO_SALE", "0").strip() == "1"

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()   # để trong file .env, KHÔNG ghi thẳng vào code

# --- Bảng ĐÍCH: "Theo dõi chia lead theo sales" ---
LARK_BASE_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN", "").strip()
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID_SALE", "").strip() or "tblWpT0rJOIiLpmP"

FIELD_SALES = "Sales"                 # cột Người (Person) -- cột duy nhất script điền khi tạo bản ghi mới
FIELD_NGAY = "Ngày"                   # cột Created time của bảng đích (dự phòng nếu API không trả created_time)
FIELD_MATCH = "Tên TK POS"
FIELD_CA = "Ca làm"                   # cột Single option: script ghi tên ca (lấy từ "Ca đăng ký")
# Mã lựa chọn của cột Ca -> tên ca (dùng khi Lark trả về mã thay vì chữ). Thêm ca mới thì thêm dòng.
MA_CA_THU_CONG = {
    "optRyyaCVZ": "08:30-18:00",
    "optoGU1CjM": "16:00-24:00",
}
FIELD_LEAD_KENH_CHUNG = "LEAD kênh chung"
FIELD_KHCU_KENH_CHUNG = "KH cũ kênh chung"
FIELD_CHUA_PHAN_HOI = "Số hội thoại chưa phản hồi"
FIELD_LEAD_ZALO_CN = "LEAD Zalo cá nhân"
FIELD_KHCU_ZALO_CN = "KH cũ zalo cá nhân"

# --- Bảng NGUỒN: "Đăng ký ca" ---
# Cùng Base với bảng đích thì không cần đặt gì thêm. Khác Base thì đặt LARK_BASE_APP_TOKEN_DANG_KY trong .env / secret.
LARK_BASE_DANG_KY = os.getenv("LARK_BASE_APP_TOKEN_DANG_KY", "").strip() or LARK_BASE_APP_TOKEN
LARK_TABLE_DANG_KY = os.getenv("LARK_TABLE_ID_DANG_KY", "").strip() or "tblverrgE0tKZS71"
FIELD_NGAY_DANG_KY = "Ngày"
FIELD_NGUOI_DANG_KY = "Người"
FIELD_CA_DANG_KY = "Ca đăng ký"
# Ca KHÔNG tính là đi làm -> không tạo bản ghi (so sánh sau khi bỏ dấu, chữ thường)
CA_KHONG_LAM = {"off", "np"}

SESSION = requests.Session()


# ── CHUNG ───────────────────────────────────────────────────────
def normalize_name(s: str) -> str:
    """Bỏ dấu, bỏ khoảng trắng thừa, chữ thường -> dùng để khớp tên."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())


def to_dt(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S") \
            .replace(tzinfo=datetime.timezone.utc).astimezone(TZ_VN)
    except Exception:
        return None


def api_get(url, params, retries=3):
    """Trả về (json, lỗi)."""
    last_error = ""
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code == 429:
                time.sleep(5); continue
            if res.status_code != 200:
                last_error = f"HTTP {res.status_code}"
                if res.status_code in (400, 401, 403, 404): break
                time.sleep(2 * (attempt + 1)); continue
            data = res.json()
            if data.get("success") is False:
                return None, f"API báo lỗi: {str(data)[:150]}"
            return data, ""
        except requests.RequestException:
            last_error = "Timeout/Lỗi mạng"
            time.sleep(2 * (attempt + 1))
    return None, last_error


def get_field(user: dict, *keys, default=0):
    for k in keys:
        if k in user:
            return user[k]
    return default


LEAD_KEYS = ("lead_kenh_chung", "khcu_kenh_chung", "lead_zalo_cn", "khcu_zalo_cn")


def new_lead() -> dict:
    return {k: 0 for k in LEAD_KEYS}


def load_channels(path: str) -> pd.DataFrame:
    """Đọc CSV, gắn cờ ca_nhan cho kênh Zalo cá nhân, lọc theo CHANNEL_WHITELIST (nếu có; kênh Zalo cá nhân luôn được giữ)."""
    df = pd.read_csv(path, dtype=str)
    df = df.dropna(subset=["page_id"]).copy()
    for c in ("page_id", "channel", "platform", "access_token"):
        df[c] = df[c].fillna("").astype(str).str.strip() if c in df else ""
    df["ca_nhan"] = df["page_id"].isin(PERSONAL_ZALO_PAGE_IDS)
    print(f"📱 {int(df['ca_nhan'].sum())} kênh Zalo cá nhân (chỉ lấy LEAD / KH cũ), {int((~df['ca_nhan']).sum())} kênh chung.")
    if CHANNEL_WHITELIST:
        wl = {(p.lower(), c.lower()) for p, c in CHANNEL_WHITELIST}
        df = df[df["ca_nhan"] | df.apply(lambda r: (r["platform"].lower(), r["channel"].lower()) in wl, axis=1)].copy()
    return df


# ── 1. TƯƠNG TÁC KHÁCH (LEAD / KH CŨ) ───────────────────────────
def fetch_engagement(page_id, token, date_range):
    data, err = api_get(f"{BASE_V1}/pages/{page_id}/statistics/customer_engagements",
                        {"page_access_token": token, "date_range": date_range, "by_hour": "false"})
    return (data or {}).get("users_engagements", []), err


# ── 2. HỘI THOẠI CHƯA ĐỌC ───────────────────────────────────────
def khach_nhan_cuoi(c, page_id) -> bool:
    """Tin cuối do KHÁCH gửi -> True. Do bất kỳ nhân viên nào của page gửi (sale, CSKH...) -> False."""
    last = c.get("last_sent_by") or {}
    if not last: return False
    if last.get("admin_id") or last.get("admin_name"): return False
    return str(last.get("id")) != str(page_id)


def fetch_unread(page_id, token, tu_luc):
    """Đếm hội thoại thỏa CẢ 2 điều kiện: chưa seen VÀ tin cuối do khách gửi.
    Trả về ({tên sale được phân công: số hội thoại}, thống kê, lỗi)."""
    users, _ = api_get(f"{BASE_V1}/pages/{page_id}/users", {"page_access_token": token})
    ten_nv = {u.get("id"): u.get("name") for u in (users or {}).get("users") or []}

    tk = {"quet": 0, "chua_seen": 0, "khach_cuoi": 0, "ca_hai": 0}
    dem, last_id = {}, None
    for _ in range(MAX_LUOT_HOI_THOAI):
        params = {"page_access_token": token}
        if last_id: params["last_conversation_id"] = last_id
        data, err = api_get(f"{BASE_V2}/pages/{page_id}/conversations", params)
        if err: return dem, tk, err
        convs = (data or {}).get("conversations") or []
        if not convs: break
        het = False
        for c in convs:
            cap_nhat = to_dt(c.get("updated_at"))
            if cap_nhat and cap_nhat < tu_luc:
                het = True; continue
            tk["quet"] += 1
            chua_seen = c.get("seen") is False
            khach_cuoi = khach_nhan_cuoi(c, page_id)
            tk["chua_seen"] += chua_seen
            tk["khach_cuoi"] += khach_cuoi
            if not (chua_seen and khach_cuoi): continue     # phải thỏa CẢ 2
            tk["ca_hai"] += 1
            ds = [u.get("name") or ten_nv.get(u.get("id")) for u in c.get("current_assign_users") or [] if isinstance(u, dict)]
            if not ds:
                ds = [ten_nv.get(i, i) for i in c.get("assignee_ids") or []]
            for ten in [x for x in ds if x] or ["(Chưa phân công)"]:
                dem[ten] = dem.get(ten, 0) + 1
        if het: break
        last_id = convs[-1].get("id")
        time.sleep(0.2)
    return dem, tk, ""


# ── XỬ LÝ 1 KÊNH ────────────────────────────────────────────────
def process_single_page(row, date_range, tu_luc):
    channel_name = f"{row['platform']} / {row['channel'] or '?'}"
    page_id, token = row["page_id"], row["access_token"]

    lead, unread = {}, {}
    if not token:
        return lead, unread, f"  ⚠️ [BỎ QUA] {channel_name}: Thiếu token."

    # 1. Lead / KH cũ: kênh chung -> 2 cột "kênh chung"; kênh Zalo cá nhân -> 2 cột "Zalo cá nhân"
    ca_nhan = bool(row["ca_nhan"])
    k_lead = "lead_zalo_cn" if ca_nhan else "lead_kenh_chung"
    k_khcu = "khcu_zalo_cn" if ca_nhan else "khcu_kenh_chung"
    records, err_lead = fetch_engagement(page_id, token, date_range)
    for user in records:
        name = user.get("name", "Không rõ")
        kh_moi = int(get_field(user, "new_customer_replied_count", "new_customer") or 0)
        total_eng = int(get_field(user, "total_engagement", "total") or 0)
        kh_cu = max(total_eng - kh_moi, 0)
        d = lead.setdefault(name, new_lead())
        d[k_lead] += kh_moi
        d[k_khcu] += kh_cu

    if ca_nhan:   # Zalo cá nhân: không đếm hội thoại chưa đọc
        lead_txt = "❌ " + err_lead if err_lead else "✅"
        return lead, unread, f"  {('[ZALO CN] ' + channel_name)[:43]:<44} {lead_txt:<6}"

    # 2. Hội thoại chưa seen & khách nhắn cuối
    unread, tk, err_unread = fetch_unread(page_id, token, tu_luc)

    lead_txt = "❌ " + err_lead if err_lead else "✅"
    if err_unread:
        hd_txt = f"❌ {err_unread}"
    else:
        hd_txt = f"{tk['quet']:>5} {tk['chua_seen']:>10} {tk['khach_cuoi']:>16} {tk['ca_hai']:>10}"
    msg = f"  {channel_name[:43]:<44} {lead_txt:<6} {hd_txt}"
    return lead, unread, msg


def fetch_all(date_range, tu_luc):
    channels = load_channels(CSV_FILE)
    print(f"✅ Bắt đầu quét {len(channels)} kênh ({MAX_WORKERS} luồng song song)...\n")
    print(f"  {'Kênh':<44} {'Lead':<6} {'Quét':>5} {'Chưa seen':>10} {'Khách nhắn cuối':>16} {'THỎA CẢ 2':>10}")
    lead_tong, unread_tong = {}, {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(process_single_page, row, date_range, tu_luc) for _, row in channels.iterrows()]
        for f in as_completed(futures):
            lead, unread, msg = f.result()
            print(msg)
            for name, vals in lead.items():
                d = lead_tong.setdefault(name, new_lead())
                for k, v in vals.items(): d[k] += v
            for name, v in unread.items():
                unread_tong[name] = unread_tong.get(name, 0) + v
    return lead_tong, unread_tong


# ── LARK: HÀM CHUNG ─────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    res = SESSION.post("https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
                       json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def _extract_text(value) -> str:
    """Ô văn bản: nối các đoạn. Ô chọn nhiều: nối bằng ' ; '."""
    if value is None: return ""
    if isinstance(value, (str, int, float)): return str(value).strip()
    if isinstance(value, dict): return str(value.get("text") or value.get("name") or "").strip()
    if isinstance(value, list):
        if all(isinstance(x, dict) and x.get("type") == "text" for x in value):
            return "".join(str(x.get("text", "")) for x in value).strip()
        return " ; ".join(_extract_text(x) for x in value)
    return str(value).strip()


def chuan_ca(text: str) -> str:
    """Đổi mã lựa chọn (optXXXX) sang tên ca nếu Lark trả về mã."""
    return re.sub(r"\bopt\w+", lambda m: MA_CA_THU_CONG.get(m.group(0), m.group(0)), text or "").strip()


def extract_nguoi(value) -> list[dict]:
    """Ô Người (Person) -> [{'id': open_id, 'name': tên}]. Lookup/ô nhiều người cũng được."""
    if isinstance(value, dict): value = [value]
    out = []
    for x in value or []:
        if isinstance(x, dict) and x.get("id"):
            out.append({"id": x["id"], "name": x.get("name") or x.get("en_name") or ""})
    return out


def parse_ngay(value):
    """Ô ngày -> datetime.date theo giờ VN. Nhận timestamp ms, 'dd/mm/yyyy', 'yyyy-mm-dd', hoặc list/dict bọc ngoài."""
    if value is None or value == "": return None
    if isinstance(value, list):
        for x in value:
            d = parse_ngay(x.get("text") if isinstance(x, dict) else x)
            if d: return d
        return None
    if isinstance(value, dict):
        return parse_ngay(value.get("value") if value.get("value") is not None else value.get("text"))
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value / 1000, TZ_VN).date()
    s = str(value).strip()
    if s.isdigit() and len(s) >= 12:
        return datetime.datetime.fromtimestamp(int(s) / 1000, TZ_VN).date()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    return None


def lark_list_items(token: str, app_token: str, table_id: str) -> list[dict]:
    """Lấy TOÀN BỘ bản ghi thô của 1 bảng (tự phân trang)."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    headers = {"Authorization": f"Bearer {token}"}
    items, page_token = [], None
    while True:
        params = {"page_size": 500, "user_id_type": "open_id"}
        if page_token: params["page_token"] = page_token
        data = SESSION.get(url, headers=headers, params=params, timeout=30).json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi (table {table_id}): {data}")
        items.extend(data["data"].get("items") or [])
        if not data["data"].get("has_more"): break
        page_token = data["data"].get("page_token")
    return items


def lark_map_lua_chon(token: str, ten_cot: str) -> dict:
    """Đổi mã lựa chọn (optXXXX) sang tên: tìm trong cấu hình cột của TẤT CẢ các bảng trong Base,
    rồi ghi đè bằng bảng khai báo tay MA_CA_THU_CONG."""
    ma = dict(MA_CA_THU_CONG)
    if ma: return ma        # đã khai báo tay thì không cần dò các bảng (đỡ mất thời gian)
    headers = {"Authorization": f"Bearer {token}"}
    base = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables"
    try:
        tables = (SESSION.get(base, headers=headers, params={"page_size": 100}, timeout=30).json()
                  .get("data") or {}).get("items") or []
        for t in tables:
            page_token = ""
            while True:
                params = {"page_size": 100}
                if page_token: params["page_token"] = page_token
                d = (SESSION.get(f"{base}/{t['table_id']}/fields", headers=headers, params=params, timeout=30)
                     .json().get("data") or {})
                for f in d.get("items") or []:
                    for o in (f.get("property") or {}).get("options") or []:
                        if o.get("id") and o.get("name"): ma.setdefault(o["id"], o["name"])
                if not d.get("has_more"): break
                page_token = d.get("page_token")
    except Exception as e:
        print(f"  (không đọc được cấu hình bảng: {type(e).__name__})")
    ma.update(MA_CA_THU_CONG)
    return ma


# ── LARK: BẢNG ĐÍCH (THEO DÕI CHIA LEAD) ────────────────────────
def lark_list_records(token: str) -> list[dict]:
    """Đọc TẤT CẢ bản ghi của bảng đích (mọi ngày), kèm ngày tạo + người (Sales)."""
    ma_ca = lark_map_lua_chon(token, FIELD_CA) if LOC_THEO_CA else {}
    records = []
    for item in lark_list_items(token, LARK_BASE_APP_TOKEN, LARK_TABLE_ID):
        fields = item.get("fields", {})
        ngay = parse_ngay(item.get("created_time")) or parse_ngay(fields.get(FIELD_NGAY))
        records.append({
            "record_id": item["record_id"],
            "ngay": ngay,
            "nguoi": extract_nguoi(fields.get(FIELD_SALES)),
            "ten_tk_pos": _extract_text(fields.get(FIELD_MATCH)),
            "ca": chuan_ca(re.sub(r"\bopt\w+", lambda m: ma_ca.get(m.group(0), m.group(0)),
                                  _extract_text(fields.get(FIELD_CA)))),
        })
    return records


def lark_batch_update(token: str, updates: list[dict]):
    if not updates: return
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        for attempt in range(3):
            try:
                data = SESSION.post(url, headers=headers, params={"user_id_type": "open_id"},
                                    json={"records": chunk}, timeout=45).json()
                if data.get("code") != 0:
                    print(f"   ❌ Lark Batch update lỗi: {data}")
                else:
                    print(f"   ✅ Lark Batch: Đã ghi thành công {len(chunk)} nhân viên.")
                break
            except requests.exceptions.ReadTimeout:
                print(f"   ⚠️ Lark API Timeout, thử lại lần {attempt + 1}...")
                time.sleep(5)


def lark_batch_create(token: str, records: list[dict]) -> int:
    """Tạo bản ghi mới ở bảng đích. KHÔNG tự thử lại khi timeout (tránh tạo trùng);
    nếu lỗi, lần chạy sau sẽ tự bổ sung phần còn thiếu. Trả về số bản ghi đã tạo."""
    if not records: return 0
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_create"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    tao = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        try:
            data = SESSION.post(url, headers=headers, params={"user_id_type": "open_id"},
                                json={"records": chunk}, timeout=45).json()
            if data.get("code") != 0:
                print(f"   ❌ Lark Batch create lỗi: {data}")
            else:
                tao += len(chunk)
        except requests.RequestException as e:
            print(f"   ❌ Lark Batch create lỗi mạng ({type(e).__name__}) -- lần chạy sau sẽ kiểm tra và bổ sung.")
    return tao


# ── LARK: BẢNG NGUỒN (ĐĂNG KÝ CA) + TẠO BẢN GHI NGÀY MỚI ────────
def lay_dang_ky_hom_nay(token: str, hom_nay: datetime.date) -> list[tuple[str, str, str]]:
    """Đọc bảng Đăng ký ca -> danh sách (open_id, tên, ca) của hôm nay (bỏ OFF / NP).
    Một người đăng ký 2 ca = 2 phần tử (mỗi ca một bản ghi)."""
    items = lark_list_items(token, LARK_BASE_DANG_KY, LARK_TABLE_DANG_KY)
    cap, da_thay = [], set()
    so_dong_hom_nay, bo_qua, doc_duoc_ngay = 0, 0, 0
    for it in items:
        f = it.get("fields", {})
        d = parse_ngay(f.get(FIELD_NGAY_DANG_KY))
        if d: doc_duoc_ngay += 1
        if d != hom_nay: continue
        so_dong_hom_nay += 1
        ca = chuan_ca(_extract_text(f.get(FIELD_CA_DANG_KY)))
        if not ca or normalize_name(ca) in CA_KHONG_LAM:
            bo_qua += 1; continue
        for p in extract_nguoi(f.get(FIELD_NGUOI_DANG_KY)):
            k = (p["id"], ca)
            if k in da_thay: continue
            da_thay.add(k)
            cap.append((p["id"], p["name"] or p["id"], ca))
    if items and doc_duoc_ngay == 0:
        print(f"⚠️ Bảng Đăng ký ca có {len(items)} dòng nhưng KHÔNG đọc được cột '{FIELD_NGAY_DANG_KY}' -- kiểm tra tên cột / kiểu dữ liệu.")
    print(f"📋 Đăng ký ca hôm nay {hom_nay:%d/%m/%Y}: {so_dong_hom_nay} dòng, bỏ {bo_qua} dòng OFF/NP/trống "
          f"-> {len(cap)} ca của {len({c[0] for c in cap})} người.")
    return cap


def dam_bao_ban_ghi_hom_nay(token: str, hom_nay: datetime.date, tat_ca: list[dict]) -> int:
    """Mỗi (người, ca) đăng ký hôm nay phải có đúng 1 bản ghi hôm nay, cột 'Sales' + 'Ca làm' được điền.
      1. Đã có bản ghi cùng người + cùng ca            -> giữ nguyên
      2. Có bản ghi của người đó nhưng 'Ca làm' còn trống -> điền ca vào bản ghi đó
      3. Còn lại                                        -> tạo bản ghi mới
    Trả về số bản ghi đã tạo + đã điền ca."""
    dang_ky = lay_dang_ky_hom_nay(token, hom_nay)
    hom_nay_recs = [r for r in tat_ca if r["ngay"] == hom_nay]
    da_dung = set()

    def tim(pid, ca):
        for r in hom_nay_recs:
            if r["record_id"] in da_dung or r["ca"] != ca: continue
            if any(p["id"] == pid for p in r["nguoi"]):
                return r
        return None

    chua_khop = []
    for pid, ten, ca in dang_ky:                       # lượt 1: khớp đúng người + ca
        r = tim(pid, ca)
        if r: da_dung.add(r["record_id"])
        else: chua_khop.append((pid, ten, ca))

    dien_ca, can_tao = [], []
    for pid, ten, ca in chua_khop:                     # lượt 2: bản ghi cùng người còn trống ca
        r = tim(pid, "")
        if r:
            da_dung.add(r["record_id"])
            dien_ca.append({"record_id": r["record_id"], "fields": {FIELD_CA: ca}})
        else:
            can_tao.append((pid, ten, ca))

    if not dien_ca and not can_tao:
        print("✅ Bản ghi hôm nay đã đủ, không cần tạo / điền thêm.")
        return 0
    print(f"➕ Tạo mới {len(can_tao)} bản ghi, điền ca cho {len(dien_ca)} bản ghi sẵn có.")
    if LOG_THEO_SALE:
        print("   tạo: " + ", ".join(f"{t} ({c})" for _, t, c in can_tao))
    if DRY_RUN:
        print("   🛑 DRY_RUN=True — chưa tạo / điền."); return 0
    lark_batch_update(token, dien_ca)
    moi = [{"fields": {FIELD_SALES: [{"id": pid}], FIELD_CA: ca}} for pid, _, ca in can_tao]
    tao = lark_batch_create(token, moi)
    print(f"   ✅ Đã tạo {tao}/{len(moi)} bản ghi mới.")
    return tao + len(dien_ca)


def chon_ban_ghi_hom_nay(tat_ca: list[dict], hom_nay: datetime.date) -> list[dict]:
    """Lọc bản ghi hôm nay; nếu thiếu 'Tên TK POS' (cột chưa tự tính cho người mới) thì lấy từ bản ghi gần nhất của cùng người."""
    ten_theo_nguoi = {}
    for r in sorted(tat_ca, key=lambda x: x["ngay"] or datetime.date.min):
        if r["ten_tk_pos"]:
            for p in r["nguoi"]:
                ten_theo_nguoi[p["id"]] = r["ten_tk_pos"]
    hom_nay_recs = [dict(r) for r in tat_ca if r["ngay"] == hom_nay]
    for r in hom_nay_recs:
        if not r["ten_tk_pos"]:
            for p in r["nguoi"]:
                if p["id"] in ten_theo_nguoi:
                    r["ten_tk_pos"] = ten_theo_nguoi[p["id"]]; break
    return hom_nay_recs


def dang_trong_ca(ca_text: str, now) -> bool:
    """'08:30-18:00' / '16:00-24:00' / '22:00-06:00'. Nhiều ca thì chỉ cần trùng 1 ca."""
    phut = now.hour * 60 + now.minute
    for a, b in re.findall(r"(\d{1,2}[:h]\d{2})\s*[-–]\s*(\d{1,2}[:h]\d{2})", ca_text or ""):
        bd = int(a[:-3]) * 60 + int(a[-2:])
        kt = int(b[:-3]) * 60 + int(b[-2:])
        if (bd <= kt and bd <= phut < kt) or (bd > kt and (phut >= bd or phut < kt)):
            return True
    return False


# ── MAIN ────────────────────────────────────────────────────────
def main():
    if not LARK_APP_ID or not LARK_APP_SECRET:
        raise SystemExit("🛑 Thiếu LARK_APP_ID / LARK_APP_SECRET (secret trên GitHub hoặc file .env khi chạy máy).")
    if not LARK_BASE_APP_TOKEN:
        raise SystemExit("🛑 Thiếu LARK_BASE_APP_TOKEN (kiểm tra secret và phần env trong file yml).")
    if not PERSONAL_ZALO_PAGE_IDS:
        raise SystemExit("🛑 Thiếu PERSONAL_ZALO_PAGE_IDS — không biết kênh nào là Zalo cá nhân để bỏ qua.")
    now_vn = datetime.datetime.now(TZ_VN)
    hom_nay = now_vn.date()
    date_range = f"{now_vn:%d/%m/%Y} 00:00:00 - {now_vn:%d/%m/%Y %H:%M:%S}"
    tu_luc = now_vn - datetime.timedelta(hours=SO_GIO_CHUA_DOC)
    print(f"🚀 Chạy lúc {now_vn:%d/%m/%Y %H:%M:%S} | DRY_RUN={DRY_RUN}")
    print(f"   Lead: {date_range} | Chưa đọc: từ {tu_luc:%d/%m %H:%M}\n")

    # ── BƯỚC 1: đảm bảo đã có bản ghi hôm nay cho từng người đăng ký ca ──
    print("⏳ Đang kiểm tra bản ghi hôm nay trên Lark...")
    token = lark_get_tenant_token()
    tat_ca = lark_list_records(token)
    if tat_ca and all(r["ngay"] is None for r in tat_ca):
        raise SystemExit(f"🛑 Không đọc được ngày tạo của bản ghi nào (created_time / cột '{FIELD_NGAY}'). "
                         f"Dừng để tránh tạo trùng -- kiểm tra cột '{FIELD_NGAY}' kiểu Created time ở bảng đích.")
    if dam_bao_ban_ghi_hom_nay(token, hom_nay, tat_ca) > 0:
        time.sleep(3)                         # chờ Lark tính xong các cột công thức / lookup của bản ghi mới
        tat_ca = lark_list_records(token)
    lark_records = chon_ban_ghi_hom_nay(tat_ca, hom_nay)
    print(f"📌 Bảng đích: {len(tat_ca)} bản ghi tổng, {len(lark_records)} bản ghi của hôm nay (chỉ cập nhật các dòng này).\n")
    thieu_ten = sum(1 for r in lark_records if not r["ten_tk_pos"])
    if thieu_ten:
        print(f"⚠️ {thieu_ten} bản ghi hôm nay chưa có '{FIELD_MATCH}' -> sẽ không khớp được số liệu Pancake.")

    # ── BƯỚC 2: lấy số liệu Pancake ──
    lead_tong, unread_tong = fetch_all(date_range, tu_luc)
    print(f"\n✅ Gom xong: lead {len(lead_tong)} nhân viên")
    print(f"   Hội thoại chưa seen & khách nhắn cuối: {sum(unread_tong.values())}")
    if LOG_THEO_SALE:
        print("--- HỘI THOẠI CHƯA SEEN & KHÁCH NHẮN CUỐI, THEO SALE ĐƯỢC PHÂN CÔNG ---")
        for ten, v in sorted(unread_tong.items(), key=lambda x: -x[1]):
            print(f"  {ten}: {v}")

    # Gộp (cộng dồn) các tên chỉ khác nhau dấu / hoa-thường giữa các kênh, tránh ghi đè lẫn nhau
    lead_norm, unread_norm = {}, {}
    for k, v in lead_tong.items():
        d = lead_norm.setdefault(normalize_name(k), new_lead())
        for kk, vv in v.items(): d[kk] += vv
    for k, v in unread_tong.items():
        nk = normalize_name(k)
        unread_norm[nk] = unread_norm.get(nk, 0) + v

    con_ma = set() if not LOC_THEO_CA else {m for r in lark_records for m in re.findall(r"\bopt\w+", r["ca"])}
    if con_ma:
        print(f"⚠️ Mã ca chưa biết tên: {', '.join(sorted(con_ma))} -> thêm vào MA_CA_THU_CONG ở đầu file")

    # ── BƯỚC 3: ghi số liệu vào bản ghi HÔM NAY ──
    updates, da_khop = [], set()
    if LOG_THEO_SALE:
        print(f"\n{'Tên TK POS':<18} | Lead chung | KH cũ chung | Lead Zalo | KH cũ Zalo | Chưa phản hồi")
    for r in lark_records:
        if not r["ten_tk_pos"]: continue
        key = normalize_name(r["ten_tk_pos"])
        da_khop.add(key)

        # 1. Lead / KH cũ kênh chung: LUÔN ghi cho tất cả sale hôm nay, chưa có dữ liệu thì ghi 0
        vals = lead_norm.get(key) or new_lead()
        fields = {
            FIELD_LEAD_KENH_CHUNG: int(vals["lead_kenh_chung"]),
            FIELD_KHCU_KENH_CHUNG: int(vals["khcu_kenh_chung"]),
            FIELD_LEAD_ZALO_CN: int(vals["lead_zalo_cn"]),
            FIELD_KHCU_ZALO_CN: int(vals["khcu_zalo_cn"]),
        }

        # 2. Hội thoại chưa đọc
        if not LOC_THEO_CA or dang_trong_ca(r["ca"], now_vn):
            chua_doc = int(unread_norm.get(key, 0))      # cột dạng Số -> gửi số
            fields[FIELD_CHUA_PHAN_HOI] = chua_doc
        else:
            chua_doc = "ngoài ca" + (" (xóa)" if XOA_NGOAI_CA else "")
            if XOA_NGOAI_CA: fields[FIELD_CHUA_PHAN_HOI] = None

        if LOG_THEO_SALE:
            print(f"{r['ten_tk_pos']:<18} | {vals['lead_kenh_chung']:<10} | {vals['khcu_kenh_chung']:<11} | {vals['lead_zalo_cn']:<9} | {vals['khcu_zalo_cn']:<10} | {chua_doc}")
        updates.append({"record_id": r["record_id"], "fields": fields})

    khong_khop = sorted({k for k in list(lead_tong) + list(unread_tong)
                         if normalize_name(k) not in da_khop and k != "(Chưa phân công)"})
    if unread_tong.get("(Chưa phân công)"):
        print(f"\n(Chưa phân công): {unread_tong['(Chưa phân công)']} hội thoại chưa đọc")
    if khong_khop:
        if LOG_THEO_SALE:
            print(f"⚠️ Nhân viên Pancake không có trong cột '{FIELD_MATCH}' (hôm nay): {', '.join(khong_khop)}")
        else:
            print(f"⚠️ {len(khong_khop)} nhân viên Pancake không có trong cột '{FIELD_MATCH}' hôm nay (bật LOG_THEO_SALE=1 để xem tên)")

    if DRY_RUN:
        print("\n🛑 DRY_RUN=True — chưa ghi Lark."); return

    print("\n⏳ Đang ghi dữ liệu vào Lark Base...")
    lark_batch_update(token, updates)
    print(f"\n🎉 HOÀN THÀNH! Cập nhật {len(updates)} dòng (mất {(datetime.datetime.now(TZ_VN) - now_vn).seconds} giây)")


if __name__ == "__main__":
    main()