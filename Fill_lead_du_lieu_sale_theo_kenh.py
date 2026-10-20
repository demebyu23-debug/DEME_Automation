import os
import re
import time
import datetime
import calendar
import unicodedata
from collections import defaultdict

import requests
import pandas as pd

# ── CẤU HÌNH CHUNG ──────────────────────────────────────────────
CSV_FILE = os.getenv("PAGES_CONFIG_CSV", "pages_config_full_2.csv")
DELAY_BETWEEN_CALLS = 1.0
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))

# Chế độ hằng ngày: điền tháng của "hôm qua" (từ mùng 1 đến hôm qua).
#   -> ngày thường: tháng hiện tại; mùng 1: tháng trước (đủ cả tháng).
# Chạy bù nhiều tháng: đặt BACKFILL_FROM="1.2024" (dạng M.YYYY)
BACKFILL_FROM = os.getenv("BACKFILL_FROM", "").strip()

DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")

# Dòng "Không xác định": các page CSV không map sẽ cộng dồn vào đây
UNKNOWN_PLATFORM = "Không xác định"
UNKNOWN_CHANNEL = "Không xác định"

# ── PANCAKE ─────────────────────────────────────────────────────
BASE_URL = "https://pages.fm/api/public_api/v1"

# ── LARK BASE ───────────────────────────────────────────────────
LARK_APP_ID = os.getenv("LARK_APP_ID")
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET")   
LARK_BASE_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN")
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID")

FIELD_MONTH = "Tháng"        # text dạng "9.2026"
FIELD_PROJECT = "Dự án"
FIELD_PLATFORM = "Nền tảng"
FIELD_CHANNEL = "Tên kênh"
FIELD_LEAD = os.getenv("LARK_FIELD_LEAD", "Lead")   # cột Number ghi số lead

SESSION = requests.Session()


# ── HELPERS ─────────────────────────────────────────────────────
def norm(s) -> str:
    """Bỏ dấu, đ->d, lowercase, gộp khoảng trắng."""
    s = str(s or "").replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


UNKNOWN_KEY = (norm(UNKNOWN_PLATFORM), norm(UNKNOWN_CHANNEL))


def extract_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("name") or first.get("text") or "").strip()
        return str(first).strip()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("text") or "").strip()
    return str(value).strip()


def parse_month_key(s: str):
    m = re.match(r"^\s*0?(\d{1,2})\.(\d{4})\s*$", s or "")
    return (int(m.group(2)), int(m.group(1))) if m else None


def get_field(user: dict, *keys, default=0):
    for k in keys:
        if k in user:
            return user[k]
    return default


def plan_months():
    """Trả về list (year, month, start_date, end_date) cần chạy."""
    yesterday = (datetime.datetime.now(TZ_VN) - datetime.timedelta(days=1)).date()
    last_ym = (yesterday.year, yesterday.month)

    if BACKFILL_FROM:
        first_ym = parse_month_key(BACKFILL_FROM)
        if not first_ym:
            raise RuntimeError(f"BACKFILL_FROM sai định dạng (cần M.YYYY): {BACKFILL_FROM}")
    else:
        first_ym = last_ym

    plan, (y, m) = [], first_ym
    while (y, m) <= last_ym:
        start = datetime.date(y, m, 1)
        end = min(datetime.date(y, m, calendar.monthrange(y, m)[1]), yesterday)
        plan.append((y, m, start, end))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return plan


# ── PANCAKE ─────────────────────────────────────────────────────
def load_channels(path: str) -> list[dict]:
    """Dòng có lark_platform + lark_channel -> map đúng kênh; để trống -> gom vào 'Không xác định'.
    Bỏ dòng không có access_token. Cột lark_project (nếu có) dùng khi tạo dòng mới."""
    df = pd.read_csv(path, dtype=str).fillna("")
    for col in ("lark_platform", "lark_channel"):
        if col not in df.columns:
            raise RuntimeError(f"CSV thiếu cột '{col}'")
    has_project = "lark_project" in df.columns

    out = []
    for _, r in df.iterrows():
        token = r["access_token"].strip()
        if not token:
            print(f"⚠️  Bỏ qua {r['platform']}/{r['channel']}: chưa có access_token")
            continue
        lp, lc = r["lark_platform"].strip(), r["lark_channel"].strip()
        if not (lp and lc):
            lp, lc = UNKNOWN_PLATFORM, UNKNOWN_CHANNEL
        out.append({
            "page_id": r["page_id"].strip(),
            "token": token,
            "csv_label": f"{r['platform']}/{r['channel']}",
            "lark_platform": lp,
            "lark_channel": lc,
            "lark_key": (norm(lp), norm(lc)),
            "project": r["lark_project"].strip() if has_project else "",
        })
    return out


def _fetch_engagement(page_id: str, token: str, start: datetime.date, end: datetime.date, retries=3):
    """Dự phòng: engagement theo nhân viên (chỉ khách đã được nhân viên rep -> số thấp hơn)."""
    url = f"{BASE_URL}/pages/{page_id}/statistics/customer_engagements"
    date_range = f"{start:%d/%m/%Y} 00:00:00 - {end:%d/%m/%Y} 23:59:59"
    params = {"page_access_token": token, "date_range": date_range, "by_hour": "false"}
    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=60)
            if res.status_code == 429:
                time.sleep(10)
                continue
            if res.status_code != 200:
                if res.status_code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
            data = res.json()
            if data.get("success") is False:
                return None
            users = data.get("users_engagements", []) or []
            return sum(int(get_field(u, "new_customer_replied_count", "new_customer") or 0) for u in users)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    return None


def fetch_new_customers(page_id: str, token: str, start: datetime.date, end: datetime.date, retries=3):
    """Khách mới cấp page = tổng new_customer_count từ /statistics/pages (khớp tab Thống kê > Trang). None nếu lỗi."""
    url = f"{BASE_URL}/pages/{page_id}/statistics/pages"
    since = int(datetime.datetime.combine(start, datetime.time.min, tzinfo=TZ_VN).timestamp())
    until = int(datetime.datetime.combine(end, datetime.time(23, 59, 59), tzinfo=TZ_VN).timestamp())
    params = {"page_access_token": token, "since": since, "until": until}

    for attempt in range(retries):
        try:
            res = SESSION.get(url, params=params, timeout=60)
            if res.status_code == 429:
                time.sleep(10)
                continue
            if res.status_code != 200:
                print(f"   ❌ HTTP {res.status_code}: {res.text[:150]}")
                if res.status_code >= 500:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None
            data = res.json()
            if data.get("success") is False:
                print(f"   ❌ API lỗi: {data}")
                return None

            rows = data.get("data")
            if not isinstance(rows, list):
                print(f"   ⚠️ page {page_id}: response không có 'data' dạng list -> dùng engagement (số có thể thấp hơn)")
                return _fetch_engagement(page_id, token, start, end)
            if rows and not any("new_customer_count" in r for r in rows if isinstance(r, dict)):
                print(f"   ⚠️ page {page_id}: thiếu new_customer_count -> dùng engagement (số có thể thấp hơn)")
                return _fetch_engagement(page_id, token, start, end)
            return sum(int(r.get("new_customer_count") or 0) for r in rows if isinstance(r, dict))
        except requests.RequestException as e:
            print(f"   ⚠️ Lần {attempt+1}/{retries} lỗi mạng: {e}")
            time.sleep(5 * (attempt + 1))
    return None


def fetch_range_with_fallback(page_id: str, token: str, start: datetime.date, end: datetime.date):
    """Thử cả khoảng 1 lần; nếu lỗi thì chia theo tuần, rồi theo ngày. None nếu vẫn lỗi."""
    n = fetch_new_customers(page_id, token, start, end)
    if n is not None:
        return n

    for step in (7, 1):
        print(f"   ↪ Chia nhỏ {step} ngày/lần cho page {page_id}")
        total, cur, ok = 0, start, True
        while cur <= end:
            chunk_end = min(cur + datetime.timedelta(days=step - 1), end)
            part = fetch_new_customers(page_id, token, cur, chunk_end, retries=2)
            time.sleep(DELAY_BETWEEN_CALLS)
            if part is None:
                ok = False
                break
            total += part
            cur = chunk_end + datetime.timedelta(days=1)
        if ok:
            return total
    return None


# ── LARK ────────────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    res = SESSION.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def lark_list_records(token: str) -> list[dict]:
    """Trả về record theo đúng thứ tự Lark trả về (dùng làm thứ tự tham chiếu khi tạo dòng mới)."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    records, page_token = [], None
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        data = SESSION.get(url, headers=headers, params=params, timeout=30).json()
        if data.get("code") != 0:
            raise RuntimeError(f"Lark list_records lỗi: {data}")
        for item in data["data"].get("items", []):
            f = item.get("fields", {})
            platform = extract_text(f.get(FIELD_PLATFORM))
            channel = extract_text(f.get(FIELD_CHANNEL))
            records.append({
                "record_id": item["record_id"],
                "month": parse_month_key(extract_text(f.get(FIELD_MONTH))),
                "project": extract_text(f.get(FIELD_PROJECT)),
                "key": (norm(platform), norm(channel)),
            })
        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def lark_batch_create(token: str, rows: list[dict]) -> bool:
    """rows: [{'fields': {...}}], tối đa 500/lần."""
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_create"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ok = True
    for i in range(0, len(rows), 500):
        res = SESSION.post(url, headers=headers, json={"records": rows[i:i + 500]}, timeout=60)
        data = res.json()
        if data.get("code") != 0:
            print(f"   ❌ Lark batch_create lỗi: {data}")
            ok = False
    return ok


def lark_batch_update(token: str, updates: list[dict]) -> bool:
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_update"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    ok = True
    for i in range(0, len(updates), 500):
        res = SESSION.post(url, headers=headers, json={"records": updates[i:i + 500]}, timeout=60)
        data = res.json()
        if data.get("code") != 0:
            print(f"   ❌ Lark batch_update lỗi: {data}")
            ok = False
    return ok


def ensure_month_rows(token: str, records: list[dict], channels: list[dict], y: int, m: int) -> bool:
    """Tạo các dòng còn thiếu của tháng (y, m) dựa trên CSV: mỗi cặp (lark_platform, lark_channel)
    một dòng, page không map -> dòng 'Không xác định'. Dự án tra từ các tháng cũ.
    Dòng mới được sắp theo dự án, rồi theo thứ tự nền tảng/kênh của tháng tham chiếu.
    Trả True nếu đã tạo dòng mới (cần load lại records)."""
    existing = {r["key"] for r in records if r["month"] == (y, m)}

    # Tra Dự án theo (Nền tảng, Tên kênh) từ dữ liệu cũ (tháng mới nhất thắng)
    project_lookup = {}
    for r in sorted((r for r in records if r["month"] and r["project"]), key=lambda r: r["month"]):
        project_lookup[r["key"]] = r["project"]

    # Tháng tham chiếu để lấy thứ tự: tháng (khác tháng đang tạo) có nhiều dòng nhất, hoà thì lấy tháng mới hơn
    counts = defaultdict(int)
    for r in records:
        if r["month"] and r["month"] != (y, m):
            counts[r["month"]] += 1
    key_rank, project_rank = {}, {}
    if counts:
        ref_ym = max(counts, key=lambda k: (counts[k], k))
        for r in records:  # giữ nguyên thứ tự Lark trả về
            if r["month"] == ref_ym:
                key_rank.setdefault(r["key"], len(key_rank))
                if r["project"]:
                    project_rank.setdefault(r["project"], len(project_rank))

    desired = {}
    for c in channels:
        k = c["lark_key"]
        if k not in desired:
            desired[k] = {"platform": c["lark_platform"], "channel": c["lark_channel"], "project": c["project"]}
        elif c["project"] and not desired[k]["project"]:
            desired[k]["project"] = c["project"]

    items = []
    for k, d in desired.items():
        if k in existing:
            continue
        d["project"] = d["project"] or project_lookup.get(k, "")
        d["key"] = k
        items.append(d)

    if not items:
        return False

    items.sort(key=lambda d: (
        project_rank.get(d["project"], 999),
        key_rank.get(d["key"], 999),
        d["platform"], d["channel"],
    ))

    new_rows = []
    for d in items:
        fields = {
            FIELD_MONTH: f"{m}.{y}",
            FIELD_PLATFORM: d["platform"],
            FIELD_CHANNEL: d["channel"],
        }
        if d["project"]:
            fields[FIELD_PROJECT] = d["project"]
        else:
            print(f"   ⚠️ Kênh mới '{d['platform']} | {d['channel']}' chưa có Dự án — "
                  f"thêm cột lark_project vào CSV hoặc điền tay trên Lark")
        new_rows.append({"fields": fields})

    print(f"🆕 Tháng {m}.{y}: thiếu {len(new_rows)} dòng, tạo theo danh sách kênh trong CSV")
    if DRY_RUN:
        for row in new_rows:
            f = row["fields"]
            print(f"   🛑 DRY_RUN — sẽ tạo: {f.get(FIELD_PROJECT, '?')} | {f[FIELD_PLATFORM]} | {f[FIELD_CHANNEL]}")
        return False
    if lark_batch_create(token, new_rows):
        print(f"   ✅ Đã tạo {len(new_rows)} dòng")
        return True
    return False


# ── MAIN ────────────────────────────────────────────────────────
def main():
    if not LARK_APP_SECRET:
        raise RuntimeError("Thiếu LARK_APP_SECRET — set biến môi trường / GitHub Secret trước khi chạy.")

    channels = load_channels(CSV_FILE)
    mapped_keys = {c["lark_key"] for c in channels}
    n_unknown = sum(1 for c in channels if c["lark_key"] == UNKNOWN_KEY)
    plan = plan_months()

    print(f"🚀 Chạy {len(plan)} tháng: " + ", ".join(f"{m}.{y}" for y, m, _, _ in plan) + f" | DRY_RUN={DRY_RUN}")
    print(f"✅ {len(channels)} page Pancake ({len(channels) - n_unknown} map đúng kênh, {n_unknown} gom vào '{UNKNOWN_CHANNEL}')\n")

    token = lark_get_tenant_token()
    records = lark_list_records(token)
    print(f"✅ Đã tải {len(records)} record từ Lark")

    # Tự tạo dòng cho tháng hiện tại và tháng của "hôm qua" nếu còn thiếu
    now = datetime.datetime.now(TZ_VN)
    yesterday = (now - datetime.timedelta(days=1)).date()
    created = False
    for ym in sorted({(now.year, now.month), (yesterday.year, yesterday.month)}):
        if ensure_month_rows(token, records, channels, *ym):
            created = True
    if created:
        records = lark_list_records(token)
        print(f"✅ Đã tải lại: {len(records)} record")

    # (year, month) -> lark_key -> [record_id]
    index = defaultdict(lambda: defaultdict(list))
    for r in records:
        if r["month"]:
            index[r["month"]][r["key"]].append(r["record_id"])

    for (y, m, start, end) in plan:
        print(f"\n📅 Tháng {m}.{y} ({start:%d/%m} → {end:%d/%m})")

        totals: dict[tuple, int] = defaultdict(int)
        failed: set[tuple] = set()
        for ch in channels:
            if ch["lark_key"] not in index[(y, m)]:
                continue  # tháng này Lark không có dòng cho kênh đó
            n = fetch_range_with_fallback(ch["page_id"], ch["token"], start, end)
            time.sleep(DELAY_BETWEEN_CALLS)
            if n is None:
                failed.add(ch["lark_key"])
                print(f"   ⚠️ {ch['csv_label']}: lỗi, bỏ qua (không ghi đè)")
                continue
            totals[ch["lark_key"]] += n
            tag = "  [→ Không xác định]" if ch["lark_key"] == UNKNOWN_KEY else ""
            print(f"   {ch['csv_label']:<45} → {n}{tag}")

        updates = []
        for key, rec_ids in index[(y, m)].items():
            if key in failed or key not in mapped_keys:
                continue  # lỗi API hoặc kênh không có trong CSV -> để nguyên, không ghi 0
            if key == UNKNOWN_KEY:
                # Nhiều dòng 'Không xác định' trong 1 tháng: ghi vào dòng đầu, các dòng còn lại = 0 (tránh nhân đôi)
                updates.append({"record_id": rec_ids[0], "fields": {FIELD_LEAD: int(totals.get(key, 0))}})
                for rid in rec_ids[1:]:
                    updates.append({"record_id": rid, "fields": {FIELD_LEAD: 0}})
                continue
            for rid in rec_ids:
                updates.append({"record_id": rid, "fields": {FIELD_LEAD: int(totals.get(key, 0))}})

        if not updates:
            print("   (không có gì để ghi)")
            continue
        if DRY_RUN:
            print(f"   🛑 DRY_RUN — sẽ ghi {len(updates)} record")
            continue
        if lark_batch_update(token, updates):
            print(f"   ✅ Đã ghi {len(updates)} record")

    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()
