"""ĐỒNG BỘ HỘI THOẠI TỰ NHIÊN (ORGANIC CHAT): PANCAKE -> LARK

Organic Chat = hội thoại mới trong ngày - số click từ quảng cáo.
Lấy page_id + access_token từ pages_config_full.csv, ghi vào cột "Organic Chat" trên Lark.
Mỗi ngày mỗi kênh 1 dòng (khớp theo Ngày + Tên kênh): đã có thì cập nhật số liệu,
chưa có thì tự tạo dòng mới (điền Ngày, Tên kênh, Organic Chat). Không tạo trùng.

Biến môi trường (GitHub Secrets): LARK_APP_ID, LARK_APP_SECRET
Chạy bù: RUN_DATE_FROM / RUN_DATE_TO (dd/mm/yyyy). Để trống = chạy cho hôm qua.
"""
import os, csv, sys, time, datetime, requests

# ── CẤU HÌNH ────────────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
PAGES_CSV = "pages_config_full.csv"   # cột: platform,channel,page_id,access_token
PANCAKE_URL = "https://pages.fm/api/public_api/v1"
DELAY_BETWEEN_CALLS = 0.6

RUN_DATE_FROM = os.getenv("RUN_DATE_FROM", "").strip()
RUN_DATE_TO   = os.getenv("RUN_DATE_TO", "").strip()

# Tên kênh trên Lark (đúng lựa chọn của cột "Tên kênh") -> (platform, channel) trong file CSV.
# Thứ tự trong danh sách = thứ tự dòng mới được tạo trên Lark.
KENH: list[tuple[str, tuple[str, str]]] = [
    ("FB DU", ("facebook",  "DEME Uniform")),                   # Fanpage Deme Uniform
    ("IG DU", ("instagram", "Đồng phục DEME")),                 # Instagram Đồng phục DEME
    ("DCF",   ("facebook",  "Đồng Phục Cafe - DEME Uniform")),  # Fanpage Đồng phục cafe
]

# ── LARK ────────────────────────────────────────────────────────
# Cần 2 secret: LARK_APP_ID, LARK_APP_SECRET
# Bảng này nằm ở base RIÊNG, khác base SALE - Customer data. Mã base và table ID ghi sẵn bên dưới.
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_BASE_APP_TOKEN = "Hf6Wbu9P8az6vpsjIo5lVYfpgzg"
LARK_TABLE_ID = "tblDrqwEl30IpASR"
LARK_API = "https://open.larksuite.com/open-apis"
LARK_URL = f"{LARK_API}/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"

FIELD_DATE    = "Ngày"
FIELD_CHANNEL = "Tên kênh"
FIELD_ORGANIC = "Organic Chat"   # Number

SESSION = requests.Session()


# ── HÀM CHUNG ───────────────────────────────────────────────────
def parse_date(x: str) -> datetime.date:
    return datetime.datetime.strptime(x, "%d/%m/%Y").date()

def get_days() -> list[datetime.date]:
    yesterday = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).date()
    if not RUN_DATE_FROM and not RUN_DATE_TO:
        return [yesterday]
    start = parse_date(RUN_DATE_FROM) if RUN_DATE_FROM else parse_date(RUN_DATE_TO)
    end = parse_date(RUN_DATE_TO) if RUN_DATE_TO else yesterday
    if start > end:
        raise SystemExit(f"🛑 Ngày bắt đầu {start:%d/%m/%Y} sau ngày kết thúc {end:%d/%m/%Y}.")
    return [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]

def day_range_ts(d: datetime.date) -> tuple[int, int]:
    start = datetime.datetime.combine(d, datetime.time(0, 0, 0), tzinfo=TZ_VN)
    return int(start.timestamp()), int(start.timestamp()) + 86399

def load_tokens(path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """{(platform, channel) viết thường: (page_id, access_token)}"""
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            key = ((row.get("platform") or "").strip().lower(), (row.get("channel") or "").strip().lower())
            out[key] = ((row.get("page_id") or "").strip(), (row.get("access_token") or "").strip())
    return out


# ── PANCAKE ─────────────────────────────────────────────────────
def fetch_organic_chat(page_id: str, token: str, d: datetime.date, retries=3) -> int | None:
    """Hội thoại mới - click quảng cáo trong ngày d. None nếu lỗi."""
    since, until = day_range_ts(d)
    params = {"access_token": token, "since": since, "until": until,
              "type_statistic": "day", "type": "by_time"}

    for attempt in range(retries):
        try:
            res = SESSION.get(f"{PANCAKE_URL}/pages/{page_id}/statistics/ads", params=params, timeout=30)
        except requests.RequestException as e:
            print(f"   ⚠️  Lần {attempt+1}/{retries} lỗi mạng: {type(e).__name__}")
            time.sleep(5 * (attempt + 1))
            continue
        if res.status_code == 429:
            print("   ⏳ Bị rate-limit (429), nghỉ 10s...")
            time.sleep(10)
            continue
        if res.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue
        if res.status_code != 200:
            print(f"   ❌ Pancake {res.status_code}: {res.text[:150]}")
            return None

        data = res.json()
        if data.get("success") is False:
            print(f"   ❌ Pancake báo lỗi: {data.get('message')}")
            return None

        page_stats = data.get("page_statistic") or []
        if not page_stats:
            print("   ⚠️  Pancake không trả về page_statistic.")
            return None
        total_inbox = sum(r.get("new_inbox_count", 0) for r in page_stats)

        ads_data = data.get("data", [])
        if isinstance(ads_data, dict):
            ads_data = ads_data.get("data", [])
        total_ads = sum(r.get("sum_click", 0) for r in ads_data or [])

        return max(total_inbox - total_ads, 0)

    print("   ❌ Pancake lỗi sau nhiều lần thử.")
    return None


# ── LARK ────────────────────────────────────────────────────────
def lark_headers() -> dict:
    res = SESSION.post(f"{LARK_API}/auth/v3/tenant_access_token/internal",
                       json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=15).json()
    if "tenant_access_token" not in res:
        raise SystemExit(f"🛑 Lấy Lark token thất bại — kiểm tra LARK_APP_ID/LARK_APP_SECRET. Response: {res}")
    return {"Authorization": f"Bearer {res['tenant_access_token']}"}

def lark_text(v) -> str:
    if isinstance(v, list):
        return " ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in v).strip()
    if isinstance(v, dict):
        return str(v.get("text", "")).strip()
    return str(v or "").strip()

def build_index(headers: dict) -> dict:
    """{dd/mm/yyyy: {tên kênh viết thường: record_id}}"""
    print("📦 Đang tải bảng Lark...")
    index, page_token, total = {}, "", 0
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        res = SESSION.get(LARK_URL, headers=headers, params=params, timeout=30)
        try:
            data = res.json()
        except ValueError:
            raise SystemExit(f"🛑 Lark trả HTTP {res.status_code}: {res.text[:200]}")
        if data.get("code") != 0:
            raise SystemExit(f"🛑 Lark lỗi khi tải bảng: code={data.get('code')} msg={data.get('msg')} "
                             "→ kiểm tra base token / table ID / quyền của app.")
        for item in data["data"].get("items") or []:
            total += 1
            f = item["fields"]
            raw = f.get(FIELD_DATE)
            if isinstance(raw, (int, float)):
                d_str = datetime.datetime.fromtimestamp(raw / 1000, tz=TZ_VN).strftime("%d/%m/%Y")
            else:
                d_str = lark_text(raw)
            kenh = lark_text(f.get(FIELD_CHANNEL)).lower()
            if d_str and kenh:
                index.setdefault(d_str, {})[kenh] = item["record_id"]
        if not data["data"].get("has_more"):
            break
        page_token = data["data"]["page_token"]
    print(f"✅ Đã tải {total} dòng.\n")
    return index

def lark_batch(headers: dict, action: str, records: list[dict]) -> int:
    """action = batch_create / batch_update. Trả về số dòng ghi thành công."""
    done = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        res = SESSION.post(f"{LARK_URL}/{action}", headers=headers,
                           json={"records": chunk}, timeout=30).json()
        if res.get("code") == 0:
            done += len(chunk)
        else:
            print(f"   ❌ Lark {action} lỗi: code={res.get('code')} msg={res.get('msg')}")
    return done


# ── MAIN ────────────────────────────────────────────────────────
def run():
    thieu = [k for k, v in {"LARK_APP_ID": LARK_APP_ID, "LARK_APP_SECRET": LARK_APP_SECRET}.items() if not v]
    if thieu:
        raise SystemExit(f"🛑 Thiếu biến môi trường: {', '.join(thieu)} (kiểm tra secret và phần env trong file yml).")
    if not os.path.exists(PAGES_CSV):
        raise SystemExit(f"🛑 Không tìm thấy {PAGES_CSV} (kiểm tra bước tạo file từ secret PAGES_CONFIG_CSV_B64).")

    t0 = time.time()
    days = get_days()
    print(f"🚀 Chạy cho {len(days)} ngày: {days[0]:%d/%m/%Y} → {days[-1]:%d/%m/%Y} (giờ VN)\n")

    tokens = load_tokens(PAGES_CSV)
    headers = lark_headers()
    index = build_index(headers)

    to_create, to_update, fail = [], [], 0
    for d in days:
        d_str = d.strftime("%d/%m/%Y")
        day_ms = day_range_ts(d)[0] * 1000   # giá trị cột Ngày (timestamp ms, 0h giờ VN)
        day_index = index.get(d_str, {})
        print(f"📅 {d_str} — Lark đã có {sum(1 for l, _ in KENH if l.lower() in day_index)}/{len(KENH)} dòng")

        for label, (platform, channel) in KENH:
            value = None
            page_id, token = tokens.get((platform, channel.lower()), ("", ""))
            if not page_id or not token:
                print(f"   ❌ [{label}] Không có page_id/access_token trong CSV ({platform} / {channel})")
            else:
                value = fetch_organic_chat(page_id, token, d)
                time.sleep(DELAY_BETWEEN_CALLS)
            if value is None:
                fail += 1
            else:
                print(f"   [{label}] Organic Chat = {value}")

            value_field = {} if value is None else {FIELD_ORGANIC: float(value)}
            record_id = day_index.get(label.lower())
            if record_id:
                if value_field:
                    to_update.append({"record_id": record_id, "fields": value_field})
            else:
                # Chưa có dòng -> tạo mới (lỗi Pancake thì vẫn tạo dòng, để trống số liệu)
                to_create.append({"fields": {FIELD_DATE: day_ms, FIELD_CHANNEL: label, **value_field}})

    created = lark_batch(headers, "batch_create", to_create) if to_create else 0
    updated = lark_batch(headers, "batch_update", to_update) if to_update else 0
    fail += (len(to_create) - created) + (len(to_update) - updated)
    print(f"\n🏁 Xong trong {time.time()-t0:.1f}s | 🆕 tạo {created} | ✅ cập nhật {updated} | ❌ {fail} thất bại")
    if fail > 0:
        sys.exit(1)

if __name__ == "__main__":
    run()
