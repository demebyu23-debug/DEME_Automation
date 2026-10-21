import os
import csv
import sys
import time
import datetime
import requests

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
CSV_FILE = "pages_config_full.csv"
DELAY_BETWEEN_CALLS = 1.0  # nghỉ giữa mỗi lần gọi API để tránh rate-limit
DRY_RUN = False            # True = chỉ in ra, không ghi vào Lark

# Chạy bù: đặt RUN_DATE_FROM và RUN_DATE_TO (dd/mm/yyyy). Chỉ đặt FROM = chạy từ ngày đó đến hôm qua.
# Để trống cả hai = chạy cho hôm qua (lịch hằng ngày).
RUN_DATE_FROM = os.getenv("RUN_DATE_FROM", "").strip()
RUN_DATE_TO   = os.getenv("RUN_DATE_TO", "").strip()

# ── PANCAKE ─────────────────────────────────────────────────────
# Public API: /pages/{page_id}/statistics/pages, dùng page_access_token riêng từng trang (cột access_token trong CSV)
BASE_URL = "https://pages.fm/api/public_api/v1"

# Các dòng cần có mỗi ngày trên Lark, đúng thứ tự hiển thị.
# Tên kênh = giá trị cột "Kênh" trên Lark. (platform, channel) = để tìm page trong CSV.
# None = không lấy từ Pancake, mặc định điền 0.
DAILY_ROWS: list[tuple[str, tuple[str, str] | None]] = [
    ("Không xác định",                          None),
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

SESSION = requests.Session()


# ── PANCAKE HELPERS ─────────────────────────────────────────────
def load_tokens(path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """{(platform, channel) viết thường: (page_id, access_token)}"""
    result = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = ((row.get("platform") or "").strip().lower(), (row.get("channel") or "").strip().lower())
            result[key] = ((row.get("page_id") or "").strip(), (row.get("access_token") or "").strip())
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
    """Tải toàn bộ record. Trả về {dd/mm/yyyy: {tên kênh viết thường: record_id}}."""
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
                index.setdefault(d_str, {})[kenh] = item["record_id"]

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
    yesterday = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).date()
    if not RUN_DATE_FROM and not RUN_DATE_TO:
        return [yesterday]
    start = parse_date(RUN_DATE_FROM) if RUN_DATE_FROM else parse_date(RUN_DATE_TO)
    end = parse_date(RUN_DATE_TO) if RUN_DATE_TO else yesterday
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

    for d in days:
        d_str = d.strftime("%d/%m/%Y")
        day_ms = day_range_ts(d)[0] * 1000   # giá trị cột Ngày (timestamp ms, 0h giờ VN)
        day_index = index.get(d_str, {})
        print(f"📅 {d_str} — Lark đã có {len(day_index)} dòng")

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

            record_id = day_index.get(label.lower())
            if record_id:
                if lead_field:
                    to_update.append({"record_id": record_id, "fields": lead_field})
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
