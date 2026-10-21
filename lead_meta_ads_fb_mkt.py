"""ĐỒNG BỘ TIẾP CẬN META (FACEBOOK / INSTAGRAM) -> LARK

Cách chạy:
  python meta_to_lark.py sync      Lấy tiếp cận organic / trả phí 15 ngày gần nhất của các kênh,
                                   ghi vào bảng Lark riêng của từng kênh (lịch hằng ngày).
  python meta_to_lark.py refresh   Đổi LONG_LIVED_TOKEN của Meta sang token mới và lưu lại
                                   vào GitHub Secret (lịch hằng tháng).

Biến môi trường (GitHub Secrets):
  sync:    LONG_LIVED_TOKEN, LARK_APP_ID, LARK_APP_SECRET
  refresh: LONG_LIVED_TOKEN, META_APP_ID, META_APP_SECRET, GH_TOKEN, REPO
"""
import os, sys, time, requests
from datetime import datetime, timedelta, timezone

# ══════════════════════════════════════════════════════════════
# CẤU HÌNH
# ══════════════════════════════════════════════════════════════
TZ_VN       = timezone(timedelta(hours=7))
DAYS_BACK   = 15          # lấy dữ liệu N ngày gần nhất (tính đến hôm qua)
API_VER     = "v25.0"
MAX_RETRIES = 3
RETRY_DELAY = 2
TU_TAO_DONG = True        # True = ngày nào chưa có dòng trên Lark thì tự tạo; False = chỉ cập nhật dòng có sẵn

# Lark: base Meta (base RIÊNG). Mã base và table ID ghi sẵn bên dưới.
LARK_APP_ID     = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_BASE_ID    = "POqqbPY30aMMx9s1TOHlYcc1gNf"
LARK_API        = "https://open.larksuite.com/open-apis"
COL_DATE        = "Ngày"
COL_PAID        = "Tiếp cận trả phí"

# Danh sách kênh: (tên hiển thị, loại fb/ig, Facebook Page ID, table ID trên Lark, tên cột organic)
# Kênh IG dùng Facebook Page ID của page được liên kết với tài khoản IG đó.
KENH = [
    ("FB DEME",           "fb", "100117591539391", "tblu6qHjKQF3NoX0", "Tiếp cận OG"),
    ("FB DEME Uniform",   "fb", "106870042472196", "tbl8ACabODZnNS7O", "Tiếp cận OG"),
    ("FB Đồng Phục Cafe", "fb", "383284691542310", "tblY6nRN3P7SCwiX", "Tiếp cận OG"),
    ("IG DEME Uniform",   "ig", "106870042472196", "tblQjWhydwGEgG0t", "Tiếp cận ogn"),
]

SESSION = requests.Session()


# ══════════════════════════════════════════════════════════════
# HÀM CHUNG
# ══════════════════════════════════════════════════════════════
def http(method: str, url: str, **kw) -> dict:
    """Gọi API, trả JSON. Lỗi mạng chỉ in loại lỗi (không in URL vì có thể chứa token)."""
    for attempt in range(MAX_RETRIES):
        try:
            return SESSION.request(method, url, timeout=60, **kw).json()
        except (requests.RequestException, ValueError) as e:
            print(f"   ⚠️  Lỗi mạng/phản hồi ({type(e).__name__}), thử lại {attempt+1}/{MAX_RETRIES}...")
            time.sleep(RETRY_DELAY * (attempt + 1))
    return {"error": {"message": "Lỗi mạng sau nhiều lần thử"}}

def get_dates(days_back=DAYS_BACK) -> list[str]:
    now = datetime.now(TZ_VN)
    return sorted((now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, days_back + 1))


# ══════════════════════════════════════════════════════════════
# META
# ══════════════════════════════════════════════════════════════
_page_tokens: dict = {}

def get_page_token(fb_id: str) -> str:
    """Lấy page token từ LONG_LIVED_TOKEN (gọi /me/accounts 1 lần, dùng lại cho mọi kênh)."""
    if not _page_tokens:
        res = http("GET", f"https://graph.facebook.com/{API_VER}/me/accounts",
                   params={"access_token": os.environ["LONG_LIVED_TOKEN"].strip(), "limit": 100})
        if "error" in res:
            raise RuntimeError(f"Lỗi lấy page token: {res['error'].get('message')}")
        _page_tokens.update({p["id"]: p["access_token"] for p in res.get("data", [])})
    if fb_id not in _page_tokens:
        raise RuntimeError(f"Token không có quyền truy cập Page {fb_id}")
    return _page_tokens[fb_id]

def get_ig_id(fb_id: str, page_token: str) -> str:
    res = http("GET", f"https://graph.facebook.com/{API_VER}/{fb_id}",
               params={"fields": "instagram_business_account", "access_token": page_token})
    ig = res.get("instagram_business_account")
    if not ig:
        raise RuntimeError(f"Không tìm thấy IG liên kết với Page {fb_id}")
    return ig["id"]

def meta_get(url: str, params: dict, context: str) -> dict:
    for attempt in range(MAX_RETRIES):
        res = http("GET", url, params=params)
        if "error" not in res:
            return res
        msg = str(res["error"].get("message", ""))
        if any(k in msg.lower() for k in ("unexpected error", "retry", "temporarily")):
            print(f"   ⏳ Meta chậm [{context}], thử lại {attempt+1}/{MAX_RETRIES}...")
            time.sleep(RETRY_DELAY * (attempt + 1))
        else:
            print(f"   ⚠️  [{context}]: {msg}")
            return res
    return res

def parse_value(res: dict, target_date: str):
    def to_date(s):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # end_time là đầu ngày hôm sau -> trừ 1 giây rồi đổi sang giờ VN
        return (dt - timedelta(seconds=1)).astimezone(TZ_VN).strftime("%Y-%m-%d")
    for item in res.get("data", []):
        for v in item.get("values", []):
            if to_date(v["end_time"]) == target_date:
                return v.get("value")
    for item in res.get("data", []):
        if item.get("values"):
            return item["values"][0].get("value")
    return None

def fetch_fb(page_id: str, page_token: str, d: str):
    """(organic, paid) FB 1 ngày: tổng - organic (breakdown is_from_ads) = paid. None nếu lỗi."""
    until = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    base = {"period": "day", "since": d, "until": until, "access_token": page_token}
    url = f"https://graph.facebook.com/{API_VER}/{page_id}/insights"
    res_tong = meta_get(url, {**base, "metric": "page_media_view"}, f"FB tổng {d}")
    res_org = meta_get(url, {**base, "metric": "page_media_view", "breakdown": "is_from_ads"}, f"FB organic {d}")
    if "error" in res_tong or "error" in res_org:
        return None
    tong = parse_value(res_tong, d) or 0
    organic = parse_value(res_org, d) or 0
    return organic, max(tong - organic, 0)

def fetch_ig(ig_id: str, page_token: str, d: str):
    """(organic, paid) IG 1 ngày: views theo media_product_type, AD = paid. None nếu lỗi."""
    until = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    res = meta_get(f"https://graph.facebook.com/{API_VER}/{ig_id}/insights", {
        "metric": "views", "period": "day", "metric_type": "total_value",
        "breakdown": "media_product_type", "since": d, "until": until, "access_token": page_token,
    }, f"IG views {d}")
    if "error" in res:
        return None
    organic = paid = 0
    data_arr = res.get("data", [])
    if data_arr:
        for r in (data_arr[0].get("total_value", {}).get("breakdowns", [{}]) or [{}])[0].get("results", []):
            dims = r.get("dimension_values", [])
            if dims and dims[0] == "AD":
                paid += r.get("value", 0)
            else:
                organic += r.get("value", 0)
    return organic, paid


# ══════════════════════════════════════════════════════════════
# LARK
# ══════════════════════════════════════════════════════════════
_lark = {"token": "", "exp": 0.0}

def lark_headers() -> dict:
    if time.time() >= _lark["exp"] - 60:
        res = http("POST", f"{LARK_API}/auth/v3/tenant_access_token/internal",
                   json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
        if res.get("code") != 0:
            raise RuntimeError(f"Lark auth thất bại — kiểm tra LARK_APP_ID/LARK_APP_SECRET: {res}")
        _lark.update(token=res["tenant_access_token"], exp=time.time() + res.get("expire", 7200))
    return {"Authorization": f"Bearer {_lark['token']}", "Content-Type": "application/json"}

def records_url(table_id: str) -> str:
    return f"{LARK_API}/bitable/v1/apps/{LARK_BASE_ID}/tables/{table_id}/records"

def date_index(table_id: str) -> dict:
    """{yyyy-mm-dd: record_id} của bảng."""
    index, page_token = {}, ""
    while True:
        params = {"page_size": 500}
        if page_token:
            params["page_token"] = page_token
        res = http("GET", records_url(table_id), headers=lark_headers(), params=params)
        if res.get("code") != 0:
            raise RuntimeError(f"Lark lỗi tải bảng {table_id}: code={res.get('code')} msg={res.get('msg')}")
        data = res.get("data") or {}
        for rec in data.get("items") or []:
            raw = (rec.get("fields") or {}).get(COL_DATE)
            if isinstance(raw, (int, float)):
                key = datetime.fromtimestamp(int(raw) / 1000, tz=TZ_VN).strftime("%Y-%m-%d")
            elif isinstance(raw, str):
                key = raw[:10]
            else:
                continue
            index[key] = rec["record_id"]
        if not data.get("has_more"):
            return index
        page_token = data.get("page_token")

def lark_batch(table_id: str, action: str, records: list[dict]) -> int:
    done = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        res = http("POST", f"{records_url(table_id)}/{action}", headers=lark_headers(), json={"records": chunk})
        if res.get("code") == 0:
            done += len(chunk)
        else:
            print(f"   ❌ Lark {action} lỗi: code={res.get('code')} msg={res.get('msg')}")
    return done


# ══════════════════════════════════════════════════════════════
# SYNC
# ══════════════════════════════════════════════════════════════
def sync_kenh(name, loai, fb_id, table_id, col_og, dates) -> bool:
    print(f"\n=== {name} ===")
    page_token = get_page_token(fb_id)
    target = get_ig_id(fb_id, page_token) if loai == "ig" else fb_id
    fetch = fetch_ig if loai == "ig" else fetch_fb

    index = date_index(table_id)
    to_create, to_update, fail = [], [], 0
    for d in dates:
        val = fetch(target, page_token, d)
        time.sleep(0.3)
        if val is None:
            print(f"   ❌ {d} | lỗi Meta, bỏ qua")
            fail += 1
            continue
        organic, paid = val
        print(f"   {d} | OG: {organic} | Paid: {paid}")
        fields = {col_og: organic, COL_PAID: paid}
        if d in index:
            to_update.append({"record_id": index[d], "fields": fields})
        elif TU_TAO_DONG:
            day_ms = int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ_VN).timestamp() * 1000)
            to_create.append({"fields": {COL_DATE: day_ms, **fields}})
        else:
            print(f"   ⚠️  {d} | Lark chưa có dòng ngày này, bỏ qua")
            fail += 1

    c = lark_batch(table_id, "batch_create", to_create) if to_create else 0
    u = lark_batch(table_id, "batch_update", to_update) if to_update else 0
    fail += (len(to_create) - c) + (len(to_update) - u)
    print(f"   → 🆕 tạo {c} | ✅ cập nhật {u} | ❌ {fail} thất bại")
    return fail == 0

def run_sync():
    thieu = [k for k in ("LONG_LIVED_TOKEN", "LARK_APP_ID", "LARK_APP_SECRET") if not os.getenv(k, "").strip()]
    if thieu:
        raise SystemExit(f"🛑 Thiếu biến môi trường: {', '.join(thieu)} (kiểm tra secret và phần env trong file yml).")
    dates = get_dates()
    print(f"🚀 Lấy dữ liệu {dates[0]} → {dates[-1]} cho {len(KENH)} kênh")

    ket_qua = {}
    for name, loai, fb_id, table_id, col_og in KENH:
        try:
            ket_qua[name] = sync_kenh(name, loai, fb_id, table_id, col_og, dates)
        except Exception as e:   # 1 kênh lỗi thì các kênh khác vẫn chạy
            print(f"   ❌ {name}: {e}")
            ket_qua[name] = False

    print("\n📋 TỔNG KẾT")
    for name, ok in ket_qua.items():
        print(f"   {'✅' if ok else '❌'} {name}")
    if not all(ket_qua.values()):
        sys.exit(1)


# ══════════════════════════════════════════════════════════════
# REFRESH TOKEN META -> GITHUB SECRET
# ══════════════════════════════════════════════════════════════
def run_refresh():
    from base64 import b64encode
    from nacl import encoding, public   # chỉ cần khi refresh: pip install pynacl

    need = ("LONG_LIVED_TOKEN", "META_APP_ID", "META_APP_SECRET", "GH_TOKEN", "REPO")
    thieu = [k for k in need if not os.getenv(k, "").strip()]
    if thieu:
        raise SystemExit(f"🛑 Thiếu biến môi trường: {', '.join(thieu)}")
    env = {k: os.environ[k].strip() for k in need}

    # 1. Đổi token Meta mới
    res = http("GET", f"https://graph.facebook.com/{API_VER}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": env["META_APP_ID"],
        "client_secret": env["META_APP_SECRET"], "fb_exchange_token": env["LONG_LIVED_TOKEN"],
    })
    if "error" in res:
        raise SystemExit(f"🛑 Meta API lỗi: {res['error'].get('message')}")
    if "access_token" not in res:
        raise SystemExit(f"🛑 Meta không trả access_token (keys: {list(res.keys())})")
    new_token = res["access_token"]
    print(f"::add-mask::{new_token}")   # che token trong log GitHub
    expires = res.get("expires_in")
    print("✅ Lấy token Meta mới" + (f", còn hạn ~{expires // 86400} ngày" if expires else ""))

    # 2. Mã hoá và lưu vào GitHub Secret LONG_LIVED_TOKEN
    gh = {"Authorization": f"Bearer {env['GH_TOKEN']}", "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28"}
    base = f"https://api.github.com/repos/{env['REPO']}/actions/secrets"
    pk = SESSION.get(f"{base}/public-key", headers=gh, timeout=30)
    if pk.status_code != 200:
        hints = {401: "GH_TOKEN sai/hết hạn -> tạo token mới, cập nhật secret GH_TOKEN.",
                 403: "GH_TOKEN thiếu quyền -> fine-grained cần Secrets: Read and write.",
                 404: "GH_TOKEN không được cấp quyền cho repo này, hoặc REPO sai."}
        raise SystemExit(f"🛑 GitHub API {pk.status_code}: {hints.get(pk.status_code, 'Không lấy được public key')}")
    pk = pk.json()
    sealed = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    put = SESSION.put(f"{base}/LONG_LIVED_TOKEN", headers=gh, timeout=30, json={
        "encrypted_value": b64encode(sealed.encrypt(new_token.encode())).decode(), "key_id": pk["key_id"]})
    if put.status_code not in (201, 204):
        raise SystemExit(f"🛑 Lưu secret thất bại (HTTP {put.status_code})")
    print("✅ Đã cập nhật LONG_LIVED_TOKEN trên GitHub Secrets")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if mode == "sync":
        run_sync()
    elif mode == "refresh":
        run_refresh()
    else:
        raise SystemExit("Cách dùng: python meta_to_lark.py [sync|refresh]")