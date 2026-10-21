"""
ĐỒNG BỘ PANCAKE -> LARK (bảng "Theo dõi chia lead theo sales")

Quét TẤT CẢ kênh trong pages_config_full.csv (có token), mỗi kênh lấy 2 loại dữ liệu:
  1. Tương tác khách hôm nay (00:00 -> lúc chạy): KH mới (LEAD) / KH cũ, tách Zalo cá nhân & kênh chung
     -> ghi 4 cột: LEAD Zalo cá nhân, LEAD kênh chung, KH cũ zalo cá nhân, KH cũ kênh chung
  2. Hội thoại chưa đọc VÀ tin cuối do khách gửi, trong 3 ngày gần nhất, theo sale được phân công
     -> ghi cột "Số hội thoại chưa phản hồi" cho tất cả sale có trong bảng
Khớp tên nhân viên Pancake với cột "Tên TK POS".
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
CSV_FILE = os.path.join(BASE_DIR, "pages_config_full.csv")

MAX_WORKERS = 5
DRY_RUN = False  # True = chỉ in log, không ghi vào Lark

# Để trống [] = lấy TẤT CẢ kênh trong CSV.
CHANNEL_WHITELIST: list[tuple[str, str]] = []

# Các kênh Zalo CÁ NHÂN -- nhận diện theo page_id.
# Danh sách lưu trong secret PERSONAL_ZALO_PAGE_IDS, các ID cách nhau bằng dấu phẩy.
PERSONAL_ZALO_PAGE_IDS: set[str] = {
    x.strip() for x in os.getenv("PERSONAL_ZALO_PAGE_IDS", "").split(",") if x.strip()
}

# Hội thoại chưa đọc
SO_GIO_CHUA_DOC = 72      # quét hội thoại cập nhật trong N tiếng gần nhất (72 = 3 ngày)
MAX_LUOT_HOI_THOAI = 30   # tối đa số lần gọi API hội thoại / kênh (60 hội thoại / lần)
LOC_THEO_CA = False       # False = điền cho TẤT CẢ sale có trong bảng; True = chỉ sale đang trong ca
XOA_NGOAI_CA = False
# Repo public: log GitHub Actions ai cũng xem được -> mặc định KHÔNG in số liệu theo tên sale.
# Muốn xem chi tiết khi dò lỗi: đặt biến LOG_THEO_SALE=1 (hoặc chạy trên máy).
LOG_THEO_SALE = os.getenv("LOG_THEO_SALE", "0").strip() == "1"      # True = sale ngoài ca bị xóa trống ô "Số hội thoại chưa phản hồi"

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()   # để trong file .env, KHÔNG ghi thẳng vào code

LARK_BASE_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN", "").strip()
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID_SALE", "").strip() or "tblWpT0rJOIiLpmP"

FIELD_MATCH = "Tên TK POS"
FIELD_CA = "Ca đăng ký"
# Mã lựa chọn của cột Ca -> tên ca (dùng khi Lark trả về mã thay vì chữ). Thêm ca mới thì thêm dòng.
MA_CA_THU_CONG = {
    "optRyyaCVZ": "08:30-18:00",
    "optoGU1CjM": "16:00-24:00",
}
FIELD_LEAD_ZALO_RIENG = "LEAD Zalo cá nhân"
FIELD_LEAD_KENH_CHUNG = "LEAD kênh chung"
FIELD_KHCU_ZALO_RIENG = "KH cũ zalo cá nhân"
FIELD_KHCU_KENH_CHUNG = "KH cũ kênh chung"
FIELD_CHUA_PHAN_HOI = "Số hội thoại chưa phản hồi"

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


def load_channels(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str)
    df = df.dropna(subset=["page_id"]).copy()
    for c in ("page_id", "channel", "platform", "access_token"):
        df[c] = df[c].fillna("").astype(str).str.strip() if c in df else ""
    if CHANNEL_WHITELIST:
        wl = {(p.lower(), c.lower()) for p, c in CHANNEL_WHITELIST}
        df = df[df.apply(lambda r: (r["platform"].lower(), r["channel"].lower()) in wl, axis=1)].copy()
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
    is_personal_zalo = page_id in PERSONAL_ZALO_PAGE_IDS
    col_type = "Zalo Cá Nhân" if is_personal_zalo else "Kênh Chung"

    lead, unread = {}, {}
    if not token:
        return lead, unread, f"  ⚠️ [BỎ QUA] {channel_name}: Thiếu token."

    # 1. Lead / KH cũ
    records, err_lead = fetch_engagement(page_id, token, date_range)
    for user in records:
        name = user.get("name", "Không rõ")
        kh_moi = int(get_field(user, "new_customer_replied_count", "new_customer") or 0)
        total_eng = int(get_field(user, "total_engagement", "total") or 0)
        kh_cu = max(total_eng - kh_moi, 0)
        d = lead.setdefault(name, {"lead_zalo_ca_nhan": 0, "lead_kenh_khac": 0,
                                   "khcu_zalo_ca_nhan": 0, "khcu_kenh_khac": 0})
        if is_personal_zalo:
            d["lead_zalo_ca_nhan"] += kh_moi; d["khcu_zalo_ca_nhan"] += kh_cu
        else:
            d["lead_kenh_khac"] += kh_moi; d["khcu_kenh_khac"] += kh_cu

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
                d = lead_tong.setdefault(name, {k: 0 for k in vals})
                for k, v in vals.items(): d[k] += v
            for name, v in unread.items():
                unread_tong[name] = unread_tong.get(name, 0) + v
    return lead_tong, unread_tong


# ── LARK ────────────────────────────────────────────────────────
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
        print(f"  (không đọc được cấu hình bảng: {e})")
    ma.update(MA_CA_THU_CONG)
    return ma


def lark_list_records(token: str) -> list[dict]:
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    ma_ca = lark_map_lua_chon(token, FIELD_CA) if LOC_THEO_CA else {}
    records, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token: params["page_token"] = page_token
        data = SESSION.get(url, headers=headers, params=params, timeout=30).json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi: {data}")
        for item in data["data"].get("items") or []:
            fields = item.get("fields", {})
            records.append({
                "record_id": item["record_id"],
                "ten_tk_pos": _extract_text(fields.get(FIELD_MATCH)),
                "ca": re.sub(r"\bopt\w+", lambda m: ma_ca.get(m.group(0), m.group(0)),
                             _extract_text(fields.get(FIELD_CA))),
            })
        if not data["data"].get("has_more"): break
        page_token = data["data"].get("page_token")
    return records


def lark_batch_update(token: str, updates: list[dict]):
    if not updates: return
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        for attempt in range(3):
            try:
                data = SESSION.post(url, headers=headers, json={"records": chunk}, timeout=45).json()
                if data.get("code") != 0:
                    print(f"   ❌ Lark Batch update lỗi: {data}")
                else:
                    print(f"   ✅ Lark Batch: Đã ghi thành công {len(chunk)} nhân viên.")
                break
            except requests.exceptions.ReadTimeout:
                print(f"   ⚠️ Lark API Timeout, thử lại lần {attempt + 1}...")
                time.sleep(5)


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
        raise SystemExit("🛑 Thiếu PERSONAL_ZALO_PAGE_IDS — nếu chạy tiếp, toàn bộ Zalo cá nhân sẽ bị tính vào kênh chung.")
    now_vn = datetime.datetime.now(TZ_VN)
    date_range = f"{now_vn:%d/%m/%Y} 00:00:00 - {now_vn:%d/%m/%Y %H:%M:%S}"
    tu_luc = now_vn - datetime.timedelta(hours=SO_GIO_CHUA_DOC)
    print(f"🚀 Chạy lúc {now_vn:%d/%m/%Y %H:%M:%S} | DRY_RUN={DRY_RUN}")
    print(f"   Lead: {date_range} | Chưa đọc: từ {tu_luc:%d/%m %H:%M}\n")

    lead_tong, unread_tong = fetch_all(date_range, tu_luc)
    print(f"\n✅ Gom xong: lead {len(lead_tong)} nhân viên")
    print(f"   Hội thoại chưa seen & khách nhắn cuối: {sum(unread_tong.values())}")
    if LOG_THEO_SALE:
        print("--- HỘI THOẠI CHƯA SEEN & KHÁCH NHẮN CUỐI, THEO SALE ĐƯỢC PHÂN CÔNG ---")
        for ten, v in sorted(unread_tong.items(), key=lambda x: -x[1]):
            print(f"  {ten}: {v}")

    print("⏳ Đang lấy danh sách record trên Lark...")
    token = lark_get_tenant_token()
    lark_records = lark_list_records(token)

    lead_norm = {normalize_name(k): v for k, v in lead_tong.items()}
    unread_norm = {normalize_name(k): v for k, v in unread_tong.items()}

    con_ma = set() if not LOC_THEO_CA else {m for r in lark_records for m in re.findall(r"\bopt\w+", r["ca"])}
    if con_ma:
        print(f"⚠️ Mã ca chưa biết tên: {', '.join(sorted(con_ma))} -> thêm vào MA_CA_THU_CONG ở đầu file")

    updates, da_khop = [], set()
    if LOG_THEO_SALE:
        print(f"\n{'Tên TK POS':<18} | Lead chung/ZaloCN | KH cũ chung/ZaloCN | Chưa phản hồi")
    for r in lark_records:
        if not r["ten_tk_pos"]: continue
        key = normalize_name(r["ten_tk_pos"])
        da_khop.add(key)
        fields = {}

        # 1. Lead / KH cũ: LUÔN ghi cho tất cả sale, chưa có dữ liệu hôm nay thì ghi 0
        vals = lead_norm.get(key) or {"lead_zalo_ca_nhan": 0, "lead_kenh_khac": 0,
                                      "khcu_zalo_ca_nhan": 0, "khcu_kenh_khac": 0}
        if True:
            fields.update({
                FIELD_LEAD_ZALO_RIENG: int(vals["lead_zalo_ca_nhan"]),
                FIELD_LEAD_KENH_CHUNG: int(vals["lead_kenh_khac"]),
                FIELD_KHCU_ZALO_RIENG: int(vals["khcu_zalo_ca_nhan"]),
                FIELD_KHCU_KENH_CHUNG: int(vals["khcu_kenh_khac"]),
            })

        # 2. Hội thoại chưa đọc: chỉ sale đang trong ca
        if not LOC_THEO_CA or dang_trong_ca(r["ca"], now_vn):
            chua_doc = int(unread_norm.get(key, 0))      # cột dạng Số -> gửi số
            fields[FIELD_CHUA_PHAN_HOI] = chua_doc
        else:
            chua_doc = "ngoài ca" + (" (xóa)" if XOA_NGOAI_CA else "")
            if XOA_NGOAI_CA: fields[FIELD_CHUA_PHAN_HOI] = None

        lead_txt = f"{vals['lead_kenh_khac']}/{vals['lead_zalo_ca_nhan']}" if vals else "-"
        khcu_txt = f"{vals['khcu_kenh_khac']}/{vals['khcu_zalo_ca_nhan']}" if vals else "-"
        if LOG_THEO_SALE:
            print(f"{r['ten_tk_pos']:<18} | {lead_txt:<17} | {khcu_txt:<18} | {chua_doc}")
        if fields:
            updates.append({"record_id": r["record_id"], "fields": fields})

    khong_khop = sorted({k for k in list(lead_tong) + list(unread_tong)
                         if normalize_name(k) not in da_khop and k != "(Chưa phân công)"})
    if unread_tong.get("(Chưa phân công)"):
        print(f"\n(Chưa phân công): {unread_tong['(Chưa phân công)']} hội thoại chưa đọc")
    if khong_khop:
        if LOG_THEO_SALE:
            print(f"⚠️ Nhân viên Pancake không có trong cột '{FIELD_MATCH}': {', '.join(khong_khop)}")
        else:
            print(f"⚠️ {len(khong_khop)} nhân viên Pancake không có trong cột '{FIELD_MATCH}' (bật LOG_THEO_SALE=1 để xem tên)")

    if DRY_RUN:
        print("\n🛑 DRY_RUN=True — chưa ghi Lark."); return

    print("\n⏳ Đang ghi dữ liệu vào Lark Base...")
    lark_batch_update(token, updates)
    print(f"\n🎉 HOÀN THÀNH! Cập nhật {len(updates)} dòng (mất {(datetime.datetime.now(TZ_VN) - now_vn).seconds} giây)")


if __name__ == "__main__":
    main()
