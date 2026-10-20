import time
import datetime
import requests
import pandas as pd
import os

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
CSV_FILE = "pages_config_full.csv"
DELAY_BETWEEN_CALLS = 1.0  # nghỉ giữa mỗi lần gọi API để tránh rate-limit

# Khoảng ngày cần đổ dữ liệu (dd/mm/yyyy).
# TỰ ĐỘNG CHỈ LẤY DỮ LIỆU CỦA ĐÚNG NGÀY HÔM QUA
YESTERDAY_STR = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).strftime("%d/%m/%Y")
START_DATE_STR = YESTERDAY_STR
END_DATE_STR = YESTERDAY_STR

DRY_RUN = False  # True = chỉ in ra, không ghi vào Lark. Đổi True để chạy thử trước.

# ── PANCAKE ─────────────────────────────────────────────────────
# Public API chính thức (developer.pancake.biz): /pages/{page_id}/statistics/pages
# Dùng page_access_token RIÊNG TỪNG TRANG (cột "access_token" trong CSV) — token này
# không hết hạn theo phiên đăng nhập như session token, nên không cần lo bị 401 do logout.
BASE_URL = "https://pages.fm/api/public_api/v1"

# Danh sách 5 kênh chuẩn
CHANNEL_WHITELIST: list[tuple[str, str]] = [
    ("shopee", "DEME_STORE"),
    ("shopee", "Deme By You"),
    ("shopee", "Deme Uniform"),
    ("shopee", "Đồng Phục In Theo Yêu Cầu DEME"),
    ("tiktokshop", "Deme Đồng Phục"),
]

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = os.getenv("LARK_APP_ID")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET")

LARK_BASE_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN")
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID")

FIELD_DATE = "Ngày"       # Tên cột Ngày
FIELD_CHANNEL = "Kênh"    # Tên cột Kênh
FIELD_LEAD = "Lead"       # Tên cột điền số liệu (Định dạng Number)

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


def build_channel_page_map(path: str) -> dict[str, tuple[str, str]]:
    """{'Shopee / DEME_STORE': (page_id, page_access_token), ...}"""
    df = load_channels(path)
    result = {}
    for _, row in df.iterrows():
        label = f"{row['platform'].capitalize()} / {row['channel']}"
        result[label] = (row["page_id"], row["access_token"])
    return result


def _find_record_list(data) -> list[dict]:
    """Response chuẩn là {'data': [...], 'success': true}. Xử lý luôn các dạng bọc khác cho chắc."""
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


def fetch_new_customers(page_id: str, token: str, date_str: str, retries=3) -> int:
    """GET /pages/{page_id}/statistics/pages -- tổng new_customer_count trong ngày date_str (dd/mm/yyyy).
    Dùng page_access_token riêng của trang, since/until là Unix timestamp UTC+0."""
    if not token:
        print(f"   ⚠️ Thiếu page_access_token cho {page_id} — bỏ qua.")
        return 0

    start_vn = datetime.datetime.strptime(date_str, "%d/%m/%Y").replace(tzinfo=TZ_VN)
    end_vn = start_vn + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    since = int(start_vn.timestamp())
    until = int(end_vn.timestamp())

    url = f"{BASE_URL}/pages/{page_id}/statistics/pages"
    params = {"page_access_token": token, "since": since, "until": until}

    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code == 401:
                print(f"   ❌ 401 Unauthorized cho {page_id} — page_access_token sai/hết quyền.")
                return 0
            if res.status_code == 429:
                time.sleep(10)
                continue
            if res.status_code != 200:
                print(f"   ❌ HTTP {res.status_code}: {res.text[:150]}")
                if res.status_code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                return 0
            data = res.json()
            if isinstance(data, dict) and data.get("success") is False:
                print(f"   ❌ API lỗi: {data}")
                return 0
            records = _find_record_list(data)
            return sum(int(r.get("new_customer_count", 0) or 0) for r in records)
        except requests.RequestException as e:
            print(f"   ⚠️ Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
    return 0


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    res = SESSION.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def lark_list_records(token: str) -> list[dict]:
    """Lấy tất cả record, giữ nguyên định dạng gốc của cột Ngày (thường là Timestamp ms)."""
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


def tim_record_id(records: list[dict], target_date_str: str, target_kenh: str) -> str | None:
    """So khớp Ngày bằng Timestamp và Kênh bằng Text."""
    target_dt = datetime.datetime.strptime(target_date_str, "%d/%m/%Y")
    target_dt_vn = target_dt.replace(tzinfo=TZ_VN)
    target_ts_start = int(target_dt_vn.timestamp() * 1000)
    target_ts_end = target_ts_start + 86400000  # + 1 ngày (ms)

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

        for kenh_label, (page_id, page_token) in channel_page_map.items():
            kh_moi = fetch_new_customers(page_id, page_token, date_str)
            time.sleep(DELAY_BETWEEN_CALLS)
            print(f"   [{kenh_label}] Khách mới = {kh_moi}")

            record_id = tim_record_id(lark_records, date_str, kenh_label)

            if record_id:
                updates.append({"record_id": record_id, "lead": kh_moi})
            else:
                missing.append((date_str, kenh_label))

    print(f"\n📊 Tổng: {len(updates)} record đã ghép khớp thành công, {len(missing)} không tìm thấy ô để ghi.")
    if missing:
        for m in missing:
            print(f"   ⚠️  Không tìm thấy record cho: {m}")

    if DRY_RUN:
        print("\n🛑 DRY_RUN=True — chưa ghi gì vào Lark. Để ghi thực tế, hãy đổi DRY_RUN = False.")
        return

    if updates:
        print("\n⏳ Đang tiến hành GHI DỮ LIỆU vào Lark Base...")
        lark_batch_update(token, updates)
    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()
