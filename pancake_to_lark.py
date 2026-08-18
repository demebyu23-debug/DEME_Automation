import os, csv, sys, time, datetime, requests

# ── Cấu hình Pancake ──────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
BASE_URL = "https://pages.fm/api/public_api/v1"
PAGES_CSV = "pages_config_full.csv"   # cột: platform,channel,page_id,access_token
DELAY_BETWEEN_CALLS = 0.6

# ── Cấu hình Lark ─────────────────────────────────────────────
# Đọc từ biến môi trường (GitHub Secrets) — KHÔNG hardcode trong code
LARK_CREDS = {
    "app_id":     os.environ.get("LARK_APP_ID", ""),
    "app_secret": os.environ.get("LARK_APP_SECRET", ""),
}
LARK_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"
LARK_TABLE_ID  = "tblEOk6xCdVd5W8x"
LARK_URL = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"

FIELD_DATE     = "Ngày"
FIELD_PLATFORM = "Nền tảng"
FIELD_CHANNEL  = "Tên kênh"
FIELD_TOTAL    = "Tổng hội thoại"   # Number field
FIELD_QC       = "Quảng cáo"        # Text field -> phải gửi dạng chuỗi

SESSION = requests.Session()

# ── Lark helpers ────────────────────────────────────────────────
_lark_token, _lark_token_exp = "", 0.0

def get_lark_token() -> str:
    global _lark_token, _lark_token_exp
    if time.time() < _lark_token_exp - 300:
        return _lark_token
    res = SESSION.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        json=LARK_CREDS, timeout=10
    ).json()
    if "tenant_access_token" not in res:
        raise RuntimeError(f"Lấy Lark token thất bại — kiểm tra LARK_APP_ID/LARK_APP_SECRET. Response: {res}")
    _lark_token = res["tenant_access_token"]
    _lark_token_exp = time.time() + res.get("expire", 7200)
    return _lark_token

def lark_headers() -> dict:
    return {"Authorization": f"Bearer {get_lark_token()}"}

def build_date_index(date_str: str) -> dict:
    """Tải record Lark đúng ngày date_str. Trả về {(platform, channel, dd/mm/yyyy): record_id}."""
    print(f"📦 Đang tải index Lark cho ngày {date_str}...")
    index, page_token = {}, ""

    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        res_json = SESSION.get(LARK_URL, headers=lark_headers(), params=params, timeout=15).json()
        if res_json.get("code") != 0:
            raise RuntimeError(
                f"Lark trả lỗi khi tải record: code={res_json.get('code')} msg={res_json.get('msg')}\n"
                "→ Kiểm tra app đã được cấp quyền truy cập base này chưa."
            )

        data = res_json.get("data", {})
        for item in data.get("items", []):
            f = item["fields"]
            raw_channel = f.get(FIELD_CHANNEL, "")
            channel = (raw_channel[0]["text"] if isinstance(raw_channel, list) else str(raw_channel)).strip()

            raw_platform = f.get(FIELD_PLATFORM, "")
            platform = (raw_platform[0]["text"] if isinstance(raw_platform, list) else str(raw_platform)).strip().lower()

            raw_date = f.get(FIELD_DATE, "")
            if isinstance(raw_date, (int, float)):
                d_str = datetime.datetime.fromtimestamp(raw_date / 1000, tz=TZ_VN).strftime("%d/%m/%Y")
            else:
                d_str = str(raw_date)

            if channel and platform and d_str == date_str:
                index[(platform, channel, d_str)] = item["record_id"]

        if not data.get("has_more"):
            break
        page_token = data["page_token"]

    print(f"✅ Index xong: {len(index)} dòng.")
    return index

def push_to_lark(index: dict, date_str: str, platform: str, channel: str, stats: dict) -> bool:
    record_id = index.get((platform, channel, date_str))
    if not record_id:
        print(f"   ⚠️  Không tìm thấy row [{platform}/{channel}] ngày {date_str} trong Lark — bỏ qua.")
        return False

    res = SESSION.put(
        f"{LARK_URL}/{record_id}",
        headers=lark_headers(),
        json={"fields": {
            FIELD_TOTAL: float(stats["total"]),
            FIELD_QC:    float(stats["qc"]),
        }},
        timeout=10
    ).json()

    if res.get("code") == 0:
        print(f"   ✅ Lark [{platform}/{channel}] {date_str} → Tổng:{stats['total']} QC:{stats['qc']}")
        return True
    else:
        print(f"   ❌ Lark lỗi [{platform}/{channel}]: {res}")
        return False

# ── Pancake helpers ─────────────────────────────────────────────
def load_pages(path: str) -> list[dict]:
    pages = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pages.append({
                "platform":     row["platform"].strip().lower(),
                "channel":      row["channel"].strip(),
                "page_id":      row["page_id"].strip(),
                "access_token": row.get("access_token", "").strip(),
            })
    return pages

def date_to_ts(d: datetime.date) -> tuple[int, int]:
    dt = datetime.datetime.combine(d, datetime.time(0, 0, 0), tzinfo=TZ_VN)
    since = int(dt.timestamp())
    until = int(dt.replace(hour=23, minute=59, second=59).timestamp())
    return since, until

def fetch_chat_stats(page_id: str, token: str, d: datetime.date, retries=3) -> dict | None:
    since, until = date_to_ts(d)
    url = f"{BASE_URL}/pages/{page_id}/statistics/ads"

    res = None
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params={
                "access_token":   token,
                "since":          since,
                "until":          until,
                "type_statistic": "day",
                "type":           "by_time",
            }, timeout=30)
            break
        except requests.RequestException as e:
            print(f"   ⚠️  Lần {attempt+1}/{retries} lỗi mạng: {e}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return None

    if res is None:
        return None

    if res.status_code == 429:
        print("   ⏳ Bị rate-limit (429), nghỉ 10s rồi thử lại...")
        time.sleep(10)
        try:
            res = SESSION.get(url, params={
                "access_token":   token,
                "since":          since,
                "until":          until,
                "type_statistic": "day",
                "type":           "by_time",
            }, timeout=30)
        except requests.RequestException as e:
            print(f"   ❌ Lỗi mạng sau retry: {e}")
            return None

    if res.status_code != 200:
        print(f"   ❌ Pancake {res.status_code}: {res.text[:200]}")
        return None

    data = res.json()
    if data.get("success") is False:
        print(f"   ❌ Pancake báo lỗi: {data.get('message')}")
        return None

    page_stats = data.get("page_statistic", [])
    total_inbox = sum(r.get("new_inbox_count", 0) for r in page_stats)

    ads_data = data.get("data", [])
    if isinstance(ads_data, dict):
        ads_data = ads_data.get("data", [])
    total_ads_click = sum(r.get("sum_click", 0) for r in ads_data)

    organic = max(total_inbox - total_ads_click, 0)
    return {"total": total_inbox, "qc": total_ads_click, "organic": organic}

# ── Main ──────────────────────────────────────────────────────
def run():
    if not LARK_CREDS["app_id"] or not LARK_CREDS["app_secret"]:
        print("🛑 Thiếu LARK_APP_ID / LARK_APP_SECRET trong biến môi trường.")
        sys.exit(1)

    t0 = time.time()
    yesterday = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).date()
    d_str = yesterday.strftime("%d/%m/%Y")
    print(f"🚀 Chạy cho ngày: {d_str} (giờ VN)\n")

    pages = load_pages(PAGES_CSV)
    index = build_date_index(d_str)

    if not index:
        print("🛑 Index Lark trống cho ngày này — dừng lại, kiểm tra base/table/quyền truy cập.")
        sys.exit(1)

    ok = fail = skip = 0
    for i, p in enumerate(pages, start=1):
        channel, platform, page_id, token = p["channel"], p["platform"], p["page_id"], p["access_token"]
        print(f"[{i}/{len(pages)}] 🔄 {channel} ({platform})")

        if not token:
            print("   ⚠️  Thiếu access_token — bỏ qua.")
            skip += 1
            continue

        stats = fetch_chat_stats(page_id, token, yesterday)
        if stats is None:
            fail += 1
        else:
            success = push_to_lark(index, d_str, platform, channel, stats)
            ok += 1 if success else 0
            fail += 0 if success else 1

        if i < len(pages):
            time.sleep(DELAY_BETWEEN_CALLS)

    print(f"\n🏁 Xong trong {time.time()-t0:.1f}s | ✅ {ok} thành công | ❌ {fail} thất bại | ⏭️ {skip} bỏ qua (thiếu token)")

    if fail > 0:
        sys.exit(1)  # để GitHub Actions đánh dấu run này là failed, dễ nhận thông báo

if __name__ == "__main__":
    run()
