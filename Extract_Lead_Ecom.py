import os
import json
import time
import datetime
import requests
import pandas as pd

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
CSV_FILE = "pages_config_full.csv"
DELAY_BETWEEN_CALLS = 1.0  # nghỉ giữa mỗi lần gọi API để tránh rate-limit

# TỰ ĐỘNG LẤY NGÀY HÔM QUA CHO CRONJOB
YESTERDAY_STR = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).strftime("%d/%m/%Y")
START_DATE_STR = YESTERDAY_STR
END_DATE_STR = YESTERDAY_STR

# Đã bật thành False để GHI THẲNG VÀO LARK BASE
DRY_RUN = False

# ── PANCAKE ─────────────────────────────────────────────────────
BASE_URL = "https://pages.fm/api/public_api/v1"

CHANNEL_WHITELIST: list[tuple[str, str]] = [
    ("shopee", "DEME_STORE"),
    ("shopee", "Deme By You"),
    ("shopee", "Deme Uniform"),
    ("shopee", "Đồng Phục In Theo Yêu Cầu DEME"),
    ("tiktokshop", "Deme Đồng Phục"),
]

# ── LARK BASE ───────────────────────────────────────────────────
# Lấy App ID và App Secret từ biến môi trường (dùng cho GitHub Actions)
LARK_CREDS = {
    "app_id":     os.environ.get("LARK_APP_ID", ""),
    "app_secret": os.environ.get("LARK_APP_SECRET", ""),
}

LARK_BASE_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"
LARK_TABLE_ID = "tbl9EfjuWYr0NCRn"

FIELD_DATE = "Ngày"       
FIELD_CHANNEL = "Kênh"    
FIELD_LEAD = "Lead"       

SESSION = requests.Session()


# ── PANCAKE HELPERS ─────────────────────────────────────────────
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

def build_channel_page_map(path: str) -> dict[str, dict]:
    df = load_channels(path)
    result = {}
    for _, row in df.iterrows():
        label = f"{row['platform'].capitalize()} / {row['channel']}"
        result[label] = {
            "page_id": row["page_id"],
            "access_token": row["access_token"]
        }
    return result

def fetch_new_customers(page_id: str, token: str, date_str: str, retries=3) -> int:
    if not token:
        print(f"   ⚠️ Thiếu access_token cho page {page_id}. Bỏ qua.")
        return 0

    url = f"{BASE_URL}/pages/{page_id}/statistics/customer_engagements"
    date_range = f"{date_str} 00:00:00 - {date_str} 23:59:59"
    params = {"page_access_token": token, "date_range": date_range, "by_hour": "false"}
    
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code != 200:
                print(f"   ❌ HTTP {res.status_code}: {res.text[:150]}")
                time.sleep(2)
                continue
            
            data = res.json()
            if data.get("success") is False:
                print(f"   ❌ API lỗi: {data}")
                return 0
                
            users = data.get("users_engagements", [])
            kh_moi = 0
            for u in users:
                kh = u.get("new_customer_replied_count") or u.get("new_customer") or 0
                kh_moi += int(kh)
            return kh_moi

        except requests.RequestException as e:
            print(f"   ⚠️ Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
    return 0


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    # Cảnh báo sớm nếu chưa set biến môi trường
    if not LARK_CREDS["app_secret"]:
        print("⚠️ CẢNH BÁO: Chưa tìm thấy biến môi trường LARK_APP_SECRET!")

    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    # Ném thẳng cục LARK_CREDS vào json siêu gọn
    res = SESSION.post(url, json=LARK_CREDS, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]

def lark_list_records(token: str) -> list[dict]:
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        res = SESSION.get(url, headers=headers, params=params, timeout=30)
        data = res.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi: {data}")
            
        for item in data["data"]["items"]:
            fields = item.get("fields", {})
            records.append({
                "record_id": item["record_id"],
                "ngay_raw": fields.get(FIELD_DATE), 
                "kenh": str(fields.get(FIELD_CHANNEL, "")).strip(),
            })
            
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records

def tim_record_id(records: list[dict], target_date_str: str, target_kenh: str) -> str:
    target_dt = datetime.datetime.strptime(target_date_str, "%d/%m/%Y")
    target_dt_vn = target_dt.replace(tzinfo=TZ_VN)
    target_ts_start = int(target_dt_vn.timestamp() * 1000)
    target_ts_end   = target_ts_start + 86400000 

    for rec in records:
        if rec["kenh"].lower() != target_kenh.lower():
            continue 

        raw_ngay = rec["ngay_raw"]
        if raw_ngay is None:
            continue

        matched = False
        if isinstance(raw_ngay, (int, float)):
            matched = target_ts_start <= int(raw_ngay) < target_ts_end
        elif isinstance(raw_ngay, str):
            matched = target_date_str in raw_ngay

        if matched:
            return rec["record_id"]
            
    return None

def lark_batch_update(token: str, updates: list[dict]):
    if not updates:
        return
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        body = {"records": [
            {"record_id": u["record_id"], "fields": {FIELD_LEAD: int(u["lead"])}} for u in chunk
        ]}
        res = SESSION.post(url, headers=headers, json=body, timeout=30)
        data = res.json()
        if data.get("code") != 0:
            print(f"   ❌ Lark batch_update lỗi (lô {i}-{i+len(chunk)}): {data}")
        else:
            print(f"   ✅ Đã đẩy thành công {len(chunk)} record vào Lark Base.")


# ── MAIN ──────────────────────────────────────────────────────
def daterange(start_str: str, end_str: str):
    start = datetime.datetime.strptime(start_str, "%d/%m/%Y")
    end = datetime.datetime.strptime(end_str, "%d/%m/%Y")
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)

def main():
    channel_page_map = build_channel_page_map(CSV_FILE)
    print(f"✅ {len(channel_page_map)} kênh cần đổ dữ liệu: {list(channel_page_map.keys())}\n")

    print("⏳ Đang lấy danh sách record hiện có trên Lark...")
    token = lark_get_tenant_token()
    lark_records = lark_list_records(token)
    print(f"✅ Đã tải {len(lark_records)} record từ bảng Lark.\n")

    updates = []
    missing = []
    
    for d in daterange(START_DATE_STR, END_DATE_STR):
        date_str = d.strftime("%d/%m/%Y")
        print(f"📅 {date_str}")

        for kenh_label, info in channel_page_map.items():
            page_id = info["page_id"]
            page_token = info["access_token"]
            
            kh_moi = fetch_new_customers(page_id, page_token, date_str)
            time.sleep(DELAY_BETWEEN_CALLS)
            print(f"   [{kenh_label}] Khách mới = {kh_moi}")

            record_id = tim_record_id(lark_records, date_str, kenh_label)
            
            if record_id:
                updates.append({"record_id": record_id, "lead": kh_moi})
            else:
                missing.append((date_str, kenh_label))

    print(f"\n📊 Tổng: {len(updates)} record đã ghép khớp thành công, {len(missing)} không tìm thấy ô để ghi.")
    
    if DRY_RUN:
        print("\n🛑 DRY_RUN=True — chưa ghi gì vào Lark.")
        return

    if updates:
        print("\n⏳ Đang tiến hành GHI DỮ LIỆU vào Lark Base...")
        lark_batch_update(token, updates)
    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()
