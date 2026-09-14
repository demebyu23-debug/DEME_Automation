import time
import datetime
import unicodedata
import requests
import pandas as pd

# ── CẤU HÌNH ────────────────────────────────────────────────────
TZ_VN = datetime.timezone(datetime.timedelta(hours=7))
BASE_URL = "https://pages.fm/api/public_api/v1"
CSV_FILE = "pages_config_full.csv"
DELAY_BETWEEN_CALLS = 1.0

DRY_RUN = False  # True = chỉ in log, không ghi vào Lark

# Để trống [] = lấy TẤT CẢ kênh trong CSV.
CHANNEL_WHITELIST: list[tuple[str, str]] = []

# Các kênh Zalo CÁ NHÂN -- nhận diện theo page_id.
PERSONAL_ZALO_PAGE_IDS: set[str] = {
    "pzl_619701931849206575",  # Liên Deme Uniform
    "pzl_645875235372158511",  # Tú Oanh Deme Uniform
    "pzl_643063084501639983",  # Trang Deme Uniform
    "pzl_645316047834632751",  # Bảo Ngọc Deme Uniform
    "pzl_630819537503081135",  # Anh Thư - Deme Uniform
    "pzl_635041653310902447",  # Thảo Deme Uniform
    "pzl_627723319520943279",  # Khánh Hoà - Deme Uniform
    "pzl_636027472809740591",  # Phương - Deme Uniform
}

# ── LARK BASE (dùng chung app id/secret của các script cũ) ──────
LARK_APP_ID = "cli_aa817039ac4bded3"
LARK_APP_SECRET = "j9zCGmtslYVF8EfegP866bB8fkrKVOgL"

LARK_BASE_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"
LARK_TABLE_ID = "tblWpT0rJOIiLpmP"   # bảng POS/ECOM Lead

FIELD_MATCH = "Tên TK POS"           # cột dùng để khớp với tên nhân viên từ Pancake
FIELD_LEAD_ZALO_RIENG = "LEAD Zalo cá nhân"
FIELD_LEAD_KENH_CHUNG = "LEAD kênh chung"

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


def fetch_engagement_public(page_id: str, token: str, date_range: str, retries=3) -> list[dict]:
    if not token:
        return []
    url = f"{BASE_URL}/pages/{page_id}/statistics/customer_engagements"
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
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.strip().lower()


def fetch_kh_moi_by_employee(date_range: str) -> dict[str, dict]:
    """{'tên gốc từ Pancake': {'zalo_ca_nhan': x, 'kenh_khac': y}}"""
    channels = load_channels(CSV_FILE)
    print(f"✅ Đã tải {len(channels)} kênh từ '{CSV_FILE}'.\n")

    totals: dict[str, dict] = {}
    for _, row in channels.iterrows():
        channel_name = row["channel"] or "?"
        page_id, token = row["page_id"], row["access_token"]
        is_personal_zalo = page_id in PERSONAL_ZALO_PAGE_IDS
        col = "zalo_ca_nhan" if is_personal_zalo else "kenh_khac"
        print(f"⏳ [{col}] {channel_name} ({page_id})")

        if not token:
            print("   ⚠️  Thiếu access_token — bỏ qua kênh này.")
            continue

        records = fetch_engagement_public(page_id, token, date_range)
        time.sleep(DELAY_BETWEEN_CALLS)
        for user in records:
            name = user.get("name", "Không rõ")
            kh_moi = int(get_field(user, "new_customer_replied_count", "new_customer"))
            d = totals.setdefault(name, {"zalo_ca_nhan": 0, "kenh_khac": 0})
            d[col] += kh_moi

    return totals


# ── LARK HELPERS ────────────────────────────────────────────────
def lark_get_tenant_token() -> str:
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    res = SESSION.post(url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Lấy tenant_access_token thất bại: {data}")
    return data["tenant_access_token"]


def _extract_text(value) -> str:
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
    """[{'record_id':..., 'ten_tk_pos': ...}, ...]"""
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
                "ten_tk_pos": _extract_text(fields.get(FIELD_MATCH)),
            })

        if not data["data"].get("has_more"):
            break
        page_token = data["data"].get("page_token")
    return records


def lark_update_one(token: str, record_id: str, zalo_ca_nhan: int, kenh_khac: int) -> bool:
    url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_BASE_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/{record_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"fields": {
        FIELD_LEAD_ZALO_RIENG: int(zalo_ca_nhan),
        FIELD_LEAD_KENH_CHUNG: int(kenh_khac),
    }}
    res = SESSION.put(url, headers=headers, json=body, timeout=30)
    data = res.json()
    if data.get("code") != 0:
        print(f"   ❌ Lark update lỗi (record {record_id}): {data}")
        return False
    return True


# ── MAIN ──────────────────────────────────────────────────────
def main():
    now_vn = datetime.datetime.now(TZ_VN)
    today_str = now_vn.strftime("%d/%m/%Y")
    date_range = f"{today_str} 00:00:00 - {now_vn.strftime('%d/%m/%Y %H:%M:%S')}"
    print(f"🚀 Chạy lúc {now_vn.strftime('%d/%m/%Y %H:%M:%S')} | DRY_RUN={DRY_RUN}\n")

    pancake_totals = fetch_kh_moi_by_employee(date_range)
    print(f"\n✅ Tra được KH mới cho {len(pancake_totals)} nhân viên trên Pancake.")

    print("⏳ Đang lấy danh sách record hiện có trên Lark...")
    token = lark_get_tenant_token()
    lark_records = lark_list_records(token)
    print(f"✅ Đã tải {len(lark_records)} record từ bảng Lark.\n")

    # index để tra nhanh: tên đã normalize -> record_id
    lark_index = {normalize_name(r["ten_tk_pos"]): r["record_id"] for r in lark_records if r["ten_tk_pos"]}

    matched, unmatched = 0, []
    for name, vals in pancake_totals.items():
        norm = normalize_name(name)
        record_id = lark_index.get(norm)
        print(f"[{name}] Zalo cá nhân={vals['zalo_ca_nhan']} | Kênh khác={vals['kenh_khac']}", end=" ")

        if not record_id:
            print("→ ⚠️ không khớp record nào trên Lark.")
            unmatched.append(name)
            continue

        if DRY_RUN:
            print(f"→ 🛑 DRY_RUN, sẽ ghi vào record {record_id}.")
            continue

        if lark_update_one(token, record_id, vals["zalo_ca_nhan"], vals["kenh_khac"]):
            print(f"→ ✅ đã ghi vào record {record_id}.")
        matched += 1

    print(f"\n📊 Tổng: {matched} nhân viên đã ghi, {len(unmatched)} không khớp: {unmatched}")
    print("\n🎉 HOÀN THÀNH!")


if __name__ == "__main__":
    main()