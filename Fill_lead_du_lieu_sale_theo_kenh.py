import os
import time
import datetime
import unicodedata
import requests
import pandas as pd

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
CSV_FILE = os.getenv("PAGES_CONFIG_CSV", "pages_config_full.csv")
DELAY_BETWEEN_CALLS = 1.0

# Mặc định chỉ chạy cho NGÀY HÔM NAY (phù hợp chạy cron 2 lần/ngày để cập nhật dần trong ngày).
# Muốn chạy bù cho ngày khác thì set biến môi trường TARGET_DATE (dd/mm/yyyy) khi chạy tay.
TARGET_DATE_STR = os.getenv("TARGET_DATE", datetime.datetime.now(TZ_VN).strftime("%d/%m/%Y"))

# "1"/"true" = chỉ in ra, không ghi vào Lark. Mặc định False để chạy tự động qua cron.
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Chỉ lấy 2 nhân viên này (so khớp không phân biệt hoa/thường & không phân biệt dấu)
EMPLOYEE_WHITELIST = ["Quý Lê Thị", "Thanh Huyen"]

# Chỉ lấy các kênh Shopee/Tiktokshop đã dùng trước đó
CHANNEL_WHITELIST: list[tuple[str, str]] = [
    ("shopee", "DEME_STORE"),
    ("shopee", "Deme By You"),
    ("shopee", "Deme Uniform"),
    ("shopee", "Đồng Phục In Theo Yêu Cầu DEME"),
    ("tiktokshop", "Deme Đồng Phục"),
]

# ── PANCAKE (Public API, page_access_token riêng từng trang trong CSV) ──
BASE_URL = "https://pages.fm/api/public_api/v1"

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = os.getenv("LARK_APP_ID", "cli_aa817039ac4bded3")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "j9zCGmtslYVF8EfegP866bB8fkrKVOgL")  # BẮT BUỘC set qua biến môi trường/GitHub Secret

LARK_BASE_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"
LARK_TABLE_ID = "tblCAIwHiZiW8cFr"   # bảng "Lead theo nhân viên"

FIELD_DATE = "Ngày"           # cột Ngày
FIELD_EMPLOYEE = "Sale POS"   # cột Sale POS (giá trị "Quý Lê Thị" / "Thanh Huyen")
FIELD_LEAD = "Lead tiếp nhận" # cột cần điền số liệu

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


def fetch_engagement_public(page_id: str, token: str, date_str: str, retries=3) -> list[dict]:
    """GET /pages/{page_id}/statistics/customer_engagements -- trả list record theo TỪNG NHÂN VIÊN."""
    if not token:
        return []

    url = f"{BASE_URL}/pages/{page_id}/statistics/customer_engagements"
    date_range = f"{date_str} 00:00:00 - {date_str} 23:59:59"
    params = {"page_access_token": token, "date_range": date_range, "by_hour": "false"}

    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code == 429:
                time.sleep(10)
                continue
            if res.status_code != 200:
                print(f"   ❌ HTTP {res.status_code}: {res.text[:150]}")
                if res.status_code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                return []
            data = res.json()
            if data.get("success") is False:
                print(f"   ❌ API lỗi: {data}")
                return []
            return data.get("users_engagements", [])
        except requests.RequestException as e:
            print(f"   ⚠️ Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
    return []


def normalize_name(s: str) -> str:
    """Bỏ dấu tiếng Việt + lowercase + strip -- để so khớp tên bất kể có dấu hay không."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip().lower()


def fetch_lead_for_employee(date_str: str, channels: pd.DataFrame, employee: str) -> int:
    """Cộng dồn new_customer_replied_count của 1 nhân viên qua tất cả kênh trong `channels`."""
    target_norm = normalize_name(employee)
    total = 0
    for _, row in channels.iterrows():
        page_id, token = row["page_id"], row["access_token"]
        records = fetch_engagement_public(page_id, token, date_str)
        time.sleep(DELAY_BETWEEN_CALLS)
        for user in records:
            if normalize_name(str(user.get("name", ""))) == target_norm:
                total += int(get_field(user, "new_customer_replied_count", "new_customer"))
    return total


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    res = SESSION.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def _extract_text(value) -> str:
    """Field có thể là text thường, hoặc list object kiểu Person/Link Record."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("name") or first.get("text") or "").strip()
        return str(first).strip()
    return str(value).strip()


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
                "nhan_vien": _extract_text(fields.get(FIELD_EMPLOYEE)),
            })

        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def tim_record_id(records: list[dict], target_date_str: str, target_employee: str) -> str | None:
    target_dt = datetime.datetime.strptime(target_date_str, "%d/%m/%Y").replace(tzinfo=TZ_VN)
    ts_start = int(target_dt.timestamp() * 1000)
    ts_end = ts_start + 86400000

    for rec in records:
        if rec["nhan_vien"].lower() != target_employee.lower():
            continue
        raw_ngay = rec["ngay_raw"]
        if raw_ngay is None:
            continue
        matched = False
        if isinstance(raw_ngay, (int, float)):
            matched = ts_start <= int(raw_ngay) < ts_end
        elif isinstance(raw_ngay, str):
            matched = target_date_str in raw_ngay
        if matched:
            return rec["record_id"]
    return None


def lark_update_one(token: str, record_id: str, lead: int) -> bool:
    """Ghi NGAY 1 record (thay vì gom hết rồi mới batch update ở cuối)."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"fields": {FIELD_LEAD: int(lead)}}
    res = SESSION.put(url, headers=headers, json=body, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        print(f"   ❌ Lark update lỗi (record {record_id}): {data}")
        return False
    return True


# ── MAIN ──────────────────────────────────────────────────────
def main():
    if not LARK_APP_SECRET:
        raise RuntimeError("Thiếu LARK_APP_SECRET — set biến môi trường/GitHub Secret trước khi chạy.")

    channels = load_channels(CSV_FILE)
    print(f"🚀 Chạy ngày: {TARGET_DATE_STR} | DRY_RUN={DRY_RUN}")
    print(f"✅ {len(channels)} kênh Shopee/Tiktokshop sẽ được cộng gộp.")
    print(f"✅ Nhân viên: {EMPLOYEE_WHITELIST}\n")

    print("⏳ Đang lấy danh sách record hiện có trên Lark...")
    token = lark_get_tenant_token()
    lark_records = lark_list_records(token)
    print(f"✅ Đã tải {len(lark_records)} record từ bảng Lark.\n")

    for emp in EMPLOYEE_WHITELIST:
        print(f"⏳ [{emp}] đang tra Pancake...")
        lead = fetch_lead_for_employee(TARGET_DATE_STR, channels, emp)
        print(f"   → Lead = {lead}")

        record_id = tim_record_id(lark_records, TARGET_DATE_STR, emp)
        if not record_id:
            print(f"   ⚠️  Không tìm thấy record ({TARGET_DATE_STR}, {emp}) trên Lark — bỏ qua.")
            continue

        if DRY_RUN:
            print(f"   🛑 DRY_RUN — sẽ ghi {lead} vào record {record_id} (chưa ghi thật).")
            continue

        # TRA ĐẾN ĐÂU GHI ĐẾN ĐẤY: ghi ngay sau khi vừa tra xong nhân viên này,
        # không đợi tra hết rồi mới ghi hàng loạt ở cuối.
        if lark_update_one(token, record_id, lead):
            print(f"   ✅ Đã ghi {lead} vào Lark (record {record_id}).")

    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()