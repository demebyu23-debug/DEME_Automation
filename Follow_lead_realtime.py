import time
import datetime
import unicodedata
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CẤU HÌNH ────────────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
BASE_URL = "https://pages.fm/api/public_api/v1"
CSV_FILE = "pages_config_full.csv"

# Không cần thiết lập DELAY tĩnh nữa vì đã dùng ThreadPool giới hạn luồng
MAX_WORKERS = 5 

DRY_RUN = False  # True = chỉ in log, không ghi vào Lark

# Để trống [] = lấy TẤT CẢ kênh trong CSV.
CHANNEL_WHITELIST: list[tuple[str, str]] = []

# Các kênh Zalo CÁ NHÂN -- nhận diện theo page_id.
PERSONAL_ZALO_PAGE_IDS: set[str] = {
    "pzl_619701931849206575",  "pzl_645875235372158511",
    "pzl_643063084501639983",  "pzl_645316047834632751",
    "pzl_630819537503081135",  "pzl_635041653310902447",
    "pzl_627723319520943279",  "pzl_636027472809740591",
}

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = "cli_aa817039ac4bded3"
LARK_APP_SECRET = "j9zCGmtslYVF8EfegP866bB8fkrKVOgL"

LARK_BASE_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"
LARK_TABLE_ID = "tblWpT0rJOIiLpmP"

FIELD_MATCH = "Tên TK POS"           
FIELD_LEAD_ZALO_RIENG = "LEAD Zalo cá nhân"
FIELD_LEAD_KENH_CHUNG = "LEAD kênh chung"
FIELD_KHCU_ZALO_RIENG = "KH cũ zalo cá nhân"
FIELD_KHCU_KENH_CHUNG = "KH cũ kênh chung"

SESSION = requests.Session()


# ── PANCAKE HELPERS ─────────────────────────────────────────────
def get_field(user: dict, *keys, default=0):
    for k in keys:
        if k in user:
            return user[k]
    return default


def load_channels(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.dropna(subset=["page_id"]).copy()
    df["page_id"] = df["page_id"].astype(str).str.strip()
    df["channel"] = df.get("channel", "").fillna("").astype(str).str.strip()
    df["platform"] = df.get("platform", "").fillna("").astype(str).str.strip()
    df["access_token"] = df.get("access_token", "").fillna("").astype(str).str.strip()

    if CHANNEL_WHITELIST:
        wl_pairs = {(p.lower(), c.lower()) for p, c in CHANNEL_WHITELIST}
        df = df[df.apply(
            lambda r: (r["platform"].lower(), r["channel"].lower()) in wl_pairs, axis=1
        )].copy()
    return df


def fetch_engagement_public(page_id: str, token: str, date_range: str, retries=3) -> tuple[list[dict], str]:
    """Trả về (dữ_liệu, câu_thông_báo_lỗi_nếu_có)"""
    url = f"{BASE_URL}/pages/{page_id}/statistics/customer_engagements"
    params = {"page_access_token": token, "date_range": date_range, "by_hour": "false"}
    
    last_error = ""
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code == 429:
                time.sleep(5)
                continue
            if res.status_code != 200:
                last_error = f"HTTP {res.status_code}"
                time.sleep(2 * (attempt + 1))
                continue
            
            data = res.json()
            if data.get("success") is False:
                return [], f"API báo lỗi: {data}"
                
            return data.get("users_engagements", []), ""
            
        except requests.RequestException as e:
            last_error = "Timeout/Lỗi mạng"
            time.sleep(2 * (attempt + 1))
            
    return [], last_error


def process_single_page(row, date_range):
    """Hàm chạy luồng con: Kéo số liệu 1 trang và trả về Dictionary"""
    channel_name = row["channel"] or "?"
    page_id, token = row["page_id"], row["access_token"]
    is_personal_zalo = page_id in PERSONAL_ZALO_PAGE_IDS
    col_type = "Zalo Cá Nhân" if is_personal_zalo else "Kênh Chung"
    
    local_totals = {}
    if not token:
        return channel_name, local_totals, f"⚠️ [BỎ QUA] {channel_name}: Thiếu token."

    # Nhận thêm biến thông báo lỗi từ hàm fetch
    records, err_msg = fetch_engagement_public(page_id, token, date_range)
    
    for user in records:
        name = user.get("name", "Không rõ")
        kh_moi = int(get_field(user, "new_customer_replied_count", "new_customer"))
        total_eng = int(get_field(user, "total_engagement", "total"))
        kh_cu = max(total_eng - kh_moi, 0)

        d = local_totals.setdefault(name, {
            "lead_zalo_ca_nhan": 0, "lead_kenh_khac": 0, 
            "khcu_zalo_ca_nhan": 0, "khcu_kenh_khac": 0
        })
        if is_personal_zalo:
            d["lead_zalo_ca_nhan"] += kh_moi
            d["khcu_zalo_ca_nhan"] += kh_cu
        else:
            d["lead_kenh_khac"] += kh_moi
            d["khcu_kenh_khac"] += kh_cu

    # Chốt câu thông báo in ra màn hình
    if err_msg:
        status_msg = f"❌ [{col_type}] {channel_name}: Lỗi {err_msg} (Ghi nhận 0 KH)"
    else:
        status_msg = f"✅ [{col_type}] {channel_name}: Xong"

    return channel_name, local_totals, status_msg


def fetch_kh_moi_by_employee(date_range: str) -> dict[str, dict]:
    """Sử dụng đa luồng (Multi-threading) để kéo Pancake siêu tốc"""
    channels = load_channels(CSV_FILE)
    print(f"✅ Bắt đầu kéo {len(channels)} kênh (với {MAX_WORKERS} luồng song song)...\n")

    totals: dict[str, dict] = {}
    
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_single_page, row, date_range) for _, row in channels.iterrows()]
        
        for future in as_completed(futures):
            channel_name, local_totals, status_msg = future.result()
            print(status_msg)
            
            # Gộp dữ liệu từ luồng con vào biến tổng
            for name, vals in local_totals.items():
                d = totals.setdefault(name, {
                    "lead_zalo_ca_nhan": 0, "lead_kenh_khac": 0, 
                    "khcu_zalo_ca_nhan": 0, "khcu_kenh_khac": 0
                })
                d["lead_zalo_ca_nhan"] += vals["lead_zalo_ca_nhan"]
                d["lead_kenh_khac"] += vals["lead_kenh_khac"]
                d["khcu_zalo_ca_nhan"] += vals["khcu_zalo_ca_nhan"]
                d["khcu_kenh_khac"] += vals["khcu_kenh_khac"]

    return totals


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    res = SESSION.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip().lower()


def _extract_text(value) -> str:
    if value is None: return ""
    if isinstance(value, str): return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict): return str(first.get("name") or first.get("text") or "").strip()
        return str(first).strip()
    return str(value).strip()


def lark_list_records(token: str) -> list[dict]:
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token: params["page_token"] = page_token
        res = SESSION.get(url, headers=headers, params=params, timeout=30)
        data = res.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi: {data}")

        for item in data["data"]["items"]:
            fields = item.get("fields", {})
            records.append({
                "record_id": item["record_id"],
                "ten_tk_pos": _extract_text(fields.get(FIELD_MATCH)),
            })

        if not data["data"].get("has_more"): break
        page_token = data["data"].get("page_token")
    return records


def lark_batch_update(token: str, updates: list[dict]):
    """Gửi toàn bộ nhân viên lên Lark bằng 1 yêu cầu (API) duy nhất"""
    if not updates:
        return
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        body = {"records": [{"record_id": u["record_id"], "fields": u["fields"]} for u in chunk]}
        
        # Gọi thử tối đa 3 lần đề phòng Timeout
        for attempt in range(3):
            try:
                res = SESSION.post(url, headers=headers, json=body, timeout=45)
                data = res.json()
                if data.get("code") != 0:
                    print(f"   ❌ Lark Batch update lỗi: {data}")
                else:
                    print(f"   ✅ Lark Batch: Đã ghi thành công {len(chunk)} nhân viên.")
                break
            except requests.exceptions.ReadTimeout:
                print(f"   ⚠️ Lark API Timeout, đang thử lại lần {attempt + 1}...")
                time.sleep(5)


# ── MAIN ──────────────────────────────────────────────────────
def main():
    now_vn = datetime.datetime.now(TZ_VN)
    today_str = now_vn.strftime("%d/%m/%Y")
    date_range = f"{today_str} 00:00:00 - {now_vn.strftime('%d/%m/%Y %H:%M:%S')}"
    print(f"🚀 Chạy lúc {now_vn.strftime('%d/%m/%Y %H:%M:%S')} | DRY_RUN={DRY_RUN}\n")

    pancake_totals = fetch_kh_moi_by_employee(date_range)
    print(f"\n✅ Đã gom xong dữ liệu cho {len(pancake_totals)} nhân viên.")

    print("⏳ Đang lấy danh sách record hiện có trên Lark...")
    token = lark_get_tenant_token()
    lark_records = lark_list_records(token)

    lark_index = {normalize_name(r["ten_tk_pos"]): r["record_id"] for r in lark_records if r["ten_tk_pos"]}

    updates = []
    unmatched = []
    
    for name, vals in pancake_totals.items():
        norm = normalize_name(name)
        record_id = lark_index.get(norm)
        
        if not record_id:
            unmatched.append(name)
            continue
            
        updates.append({
            "record_id": record_id,
            "fields": {
                FIELD_LEAD_ZALO_RIENG: int(vals["lead_zalo_ca_nhan"]),
                FIELD_LEAD_KENH_CHUNG: int(vals["lead_kenh_khac"]),
                FIELD_KHCU_ZALO_RIENG: int(vals["khcu_zalo_ca_nhan"]),
                FIELD_KHCU_KENH_CHUNG: int(vals["khcu_kenh_khac"]),
            }
        })

    if DRY_RUN:
        print("\n🛑 DRY_RUN=True — Sẽ cập nhật cho các record sau:")
        for u in updates: print(u)
        return

    print("\n⏳ Đang tiến hành GHI DỮ LIỆU vào Lark Base (Bằng Batch Update)...")
    lark_batch_update(token, updates)

    print(f"\n📊 Tổng: Đã lên danh sách {len(updates)} nhân viên, {len(unmatched)} không khớp ({unmatched[:5]}...)")
    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()
