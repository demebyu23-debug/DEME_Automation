import os, csv, sys, time, datetime, requests

# ── Cấu hình Pancake ──────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
BASE_URL = "https://pages.fm/api/public_api/v1"
PAGES_CSV = "pages_config_full.csv"   # cột: platform,channel,page_id,access_token
DELAY_BETWEEN_CALLS = 0.6

# ── Cấu hình Lark ─────────────────────────────────────────────
# Đọc từ biến môi trường (GitHub Secrets) — KHÔNG hardcode trong code
# Cần 3 secret: LARK_APP_ID, LARK_APP_SECRET, LARK_BASE_APP_TOKEN
LARK_CREDS = {
    "app_id":     os.environ.get("LARK_APP_ID", ""),
    "app_secret": os.environ.get("LARK_APP_SECRET", ""),
}
LARK_APP_TOKEN = os.environ.get("LARK_BASE_APP_TOKEN", "").strip()
LARK_TABLE_ID  = "tblEOk6xCdVd5W8x"
LARK_URL = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"

# Chạy bù cho ngày cụ thể: đặt biến RUN_DATE = dd/mm/yyyy. Để trống = chạy cho hôm qua.
RUN_DATE = os.environ.get("RUN_DATE", "").strip()

FIELD_DATE     = "Ngày"
FIELD_PLATFORM = "Nền tảng"
FIELD_CHANNEL  = "Tên kênh"
FIELD_TOTAL    = "Tổng hội thoại"   # Number field
FIELD_QC       = "Quảng cáo"        # Number field

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

def lark_json(res: requests.Response) -> dict:
    try:
        return res.json()
    except ValueError:
        raise RuntimeError(
            f"Lark trả HTTP {res.status_code}: {res.text[:200]}\n"
            f"→ Độ dài base token = {len(LARK_APP_TOKEN)} (đúng là 27), "
            f"table ID = {len(LARK_TABLE_ID)} (đúng là 16)"
        )

def build_date_index(date_str: str) -> dict:
    """Tải record Lark đúng ngày date_str. Trả về {(platform, channel): record_id}."""
    print(f"📦 Đang tải index Lark cho ngày {date_str}...")
    index, page_token = {}, ""

    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token

        res_json = lark_json(SESSION.get(LARK_URL, headers=lark_headers(), params=params, timeout=15))
        if res_json.get("code") != 0:
            raise RuntimeError(
                f"Lark trả lỗi khi tải record: code={res_json.get('code')} msg={res_json.get('msg')}\n"
                "→ Kiểm tra app đã được cấp quyền truy cập base này chưa."
            )

        data = res_json.get("data", {})
        for item in data.get("items", []) or []:
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
                index[(platform, channel)] = item["record_id"]

        if not data.get("has_more"):
            break
        page_token = data["page_token"]

    print(f"✅ Lark đã có sẵn {len(index)} dòng cho ngày này.\n")
    return index

def lark_batch(action: str, records: list[dict]) -> int:
    """action = 'batch_create' hoặc 'batch_update'. Trả về số dòng ghi thành công."""
    done = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        res = lark_json(SESSION.post(
            f"{LARK_URL}/{action}",
            headers=lark_headers(),
            json={"records": chunk},
            timeout=30,
        ))
        if res.get("code") == 0:
            done += len(chunk)
        else:
            print(f"   ❌ Lark {action} lỗi: code={res.get('code')} msg={res.get('msg')}")
    return done

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
                "access_token": (row.get("access_token") or "").strip(),
            })
    # Sắp xếp theo nền tảng, rồi theo tên kênh -> dòng mới trên Lark ra đúng thứ tự
    pages.sort(key=lambda p: (p["platform"], p["channel"]))
    return pages

def date_to_ts(d: datetime.date) -> tuple[int, int]:
    dt = datetime.datetime.combine(d, datetime.time(0, 0, 0), tzinfo=TZ_VN)
    since = int(dt.timestamp())
    until = int(dt.replace(hour=23, minute=59, second=59).timestamp())
    return since, until

def fetch_chat_stats(page_id: str, token: str, d: datetime.date, retries=3) -> dict | None:
    since, until = date_to_ts(d)
    url = f"{BASE_URL}/pages/{page_id}/statistics/ads"
    params = {
        "access_token":   token,
        "since":          since,
        "until":          until,
        "type_statistic": "day",
        "type":           "by_time",
    }

    res = None
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"   ⚠️  Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
            continue
        if res.status_code == 429:
            print("   ⏳ Bị rate-limit (429), nghỉ 10s rồi thử lại...")
            time.sleep(10)
            continue
        break

    if res is None:
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

    return {"total": total_inbox, "qc": total_ads_click}

# ── Main ──────────────────────────────────────────────────────
def run():
    if not LARK_CREDS["app_id"] or not LARK_CREDS["app_secret"]:
        print("🛑 Thiếu LARK_APP_ID / LARK_APP_SECRET trong biến môi trường.")
        sys.exit(1)
    if not LARK_APP_TOKEN:
        print("🛑 Thiếu LARK_BASE_APP_TOKEN trong biến môi trường (kiểm tra secret và phần env trong file yml).")
        sys.exit(1)

    t0 = time.time()
    if RUN_DATE:
        run_day = datetime.datetime.strptime(RUN_DATE, "%d/%m/%Y").date()
    else:
        run_day = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).date()
    d_str = run_day.strftime("%d/%m/%Y")
    day_ms = date_to_ts(run_day)[0] * 1000   # giá trị cột Ngày (timestamp ms, 0h giờ VN)
    print(f"🚀 Chạy cho ngày: {d_str} (giờ VN)\n")

    pages = load_pages(PAGES_CSV)
    index = build_date_index(d_str)

    to_create, to_update = [], []
    fail = skip = 0

    for i, p in enumerate(pages, start=1):
        channel, platform, page_id, token = p["channel"], p["platform"], p["page_id"], p["access_token"]
        print(f"[{i}/{len(pages)}] 🔄 {channel} ({platform})")

        stats = None
        if not token:
            print("   ⚠️  Thiếu access_token — chỉ tạo dòng, không có số liệu.")
            skip += 1
        else:
            stats = fetch_chat_stats(page_id, token, run_day)
            if stats is None:
                fail += 1
            else:
                print(f"   Tổng:{stats['total']} QC:{stats['qc']}")
            if i < len(pages):
                time.sleep(DELAY_BETWEEN_CALLS)

        stat_fields = {}
        if stats is not None:
            stat_fields = {FIELD_TOTAL: float(stats["total"]), FIELD_QC: float(stats["qc"])}

        record_id = index.get((platform, channel))
        if record_id:
            if stat_fields:
                to_update.append({"record_id": record_id, "fields": stat_fields})
        else:
            to_create.append({"fields": {
                FIELD_DATE:     day_ms,
                FIELD_PLATFORM: platform,
                FIELD_CHANNEL:  channel,
                **stat_fields,
            }})

    print(f"\n📝 Ghi vào Lark: tạo mới {len(to_create)} dòng, cập nhật {len(to_update)} dòng...")
    created = lark_batch("batch_create", to_create) if to_create else 0
    updated = lark_batch("batch_update", to_update) if to_update else 0
    fail += (len(to_create) - created) + (len(to_update) - updated)

    print(
        f"\n🏁 Xong trong {time.time()-t0:.1f}s | 🆕 tạo {created} | ✅ cập nhật {updated} "
        f"| ❌ {fail} thất bại | ⏭️ {skip} thiếu token"
    )

    if fail > 0:
        sys.exit(1)  # để GitHub Actions đánh dấu run này là failed, dễ nhận thông báo

if __name__ == "__main__":
    run()
