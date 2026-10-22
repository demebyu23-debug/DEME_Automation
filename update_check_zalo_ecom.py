"""
Tự động "Đồng bộ lại thủ công" Pancake POS -> Lark Base (bảng SALE - Customer data / POS).

Làm giống thao tác tay: lấy các đơn TẠO trong N ngày gần nhất (mặc định 15 ngày) từ Pancake,
rồi ghi đè lại các cột hay thay đổi sau khi tạo đơn (trạng thái, người xử lý, thẻ, ghi chú...) lên Lark.
Các cột công thức / cột của Lark (TK LARK, CĐN, Nguồn đơn chuẩn...) KHÔNG bị đụng tới.

Chạy:
    python update_check_zalo_ecom.py --dry-run          # xem trước (đơn tạo trong 15 ngày, chỉ cột "Thẻ Pancake")
    python update_check_zalo_ecom.py                    # chạy thật, chỉ cập nhật các đơn ĐÃ CÓ trong bảng
    python update_check_zalo_ecom.py --days 3 --by updated_at --fields all   # mọi cột, theo ngày cập nhật
    python update_check_zalo_ecom.py --from 2026-10-01 --to 2026-10-22
    python update_check_zalo_ecom.py --days 3 --create-missing
    python update_check_zalo_ecom.py --dump-sample      # in JSON 1 đơn (CHỈ chạy trên máy, có thông tin khách)

Biến môi trường (GitHub Secrets): PANCAKE_API_KEY_1890165155, LARK_APP_ID, LARK_APP_SECRET
pip install requests
"""
import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ====================== CẤU HÌNH ======================
PANCAKE_BASE = "https://pos.pages.fm/api/v1"
SHOP_ID = "1890165155"
# API key riêng của shop này, đọc từ secret (KHÔNG ghi key vào code)
PANCAKE_API_KEY = os.getenv(f"PANCAKE_API_KEY_{SHOP_ID}", "").strip()

LARK_DOMAIN = "https://open.larksuite.com"
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_APP_TOKEN = "KXMMbFBQRaTdZDsB3Y8lktQygqb"   # base SALE - Customer data
LARK_TABLE_ID = "tblPrPfMbCld2DRr"               # bảng POS

# Cột khoá trên Lark: S{shop_id}O{system_id}, vd S1890165155O23034
KEY_FIELD = "Mã đơn hàng đầy đủ"

# Mặc định lọc theo NGÀY TẠO đơn ("inserted_at"). Đổi thành "updated_at" (hoặc dùng --by updated_at) để lọc theo ngày cập nhật.
UPDATE_STATUS = "inserted_at"
DEFAULT_DAYS = 15
# Mặc định chỉ cập nhật cột này. Dùng --fields all để cập nhật tất cả cột trong LATE_FIELDS.
DEFAULT_FIELDS = ["Thẻ Pancake"]
# Cột chọn (single/multi select) được phép TỰ TẠO option mới khi Pancake có giá trị mà Lark chưa có.
# Các cột chọn khác (vd Trạng thái) chỉ cảnh báo và bỏ qua, để tránh tạo option sai tên.
AUTO_CREATE_OPTIONS = {"Thẻ Pancake"}
PAGE_SIZE = 100

# Pancake trả giờ UTC không kèm múi giờ (đã đối chiếu với JSON mẫu). Giờ hiển thị trên Lark là giờ VN (UTC+7).
VN_TZ = timezone(timedelta(hours=7))
TEXT_DATE_FMT = "%Y/%m/%d %H:%M:%S"  # chỉ dùng nếu cột ngày trên Lark là cột TEXT thay vì cột Ngày

# Mã trạng thái Pancake -> chữ hiển thị trên Lark (đúng chính tả đang có trong bảng: "Mới", "Đã huỷ", "Đã xác nhận").
# Mã nào chưa có ở đây sẽ bị bỏ qua và in cảnh báo -> bổ sung vào cho đúng tên option bạn đang dùng.
STATUS_MAP = {
    0: "Mới",
    1: "Đã xác nhận",
    2: "Đã gửi hàng",
    3: "Đã nhận",
    4: "Đang trả hàng",
    5: "Đã trả hàng",
    6: "Đã huỷ",
    8: "Đang đóng hàng",
    9: "Chờ chuyển hàng",
    11: "Chờ hàng",
    12: "Chờ in",
    13: "Đã in",
    15: "Hoàn một phần",
}
# ======================================================

LOI_GHI = 0   # đếm số lô ghi Lark bị lỗi -> cuối chương trình báo đỏ trên GitHub Actions


def nested(o, *path):
    for p in path:
        if not isinstance(o, dict):
            return None
        o = o.get(p)
    return o


def conversation_tags(o):
    """Thẻ hội thoại của khách (customer.conversation_tags). Thiếu dữ liệu -> None (không ghi đè)."""
    tags = nested(o, "customer", "conversation_tags")
    return [t for t in tags if t] if isinstance(tags, list) else None


def tag_names(o):  # thẻ của ĐƠN (order.tags) - không dùng cho cột Thẻ Pancake
    return [t.get("name") for t in (o.get("tags") or []) if isinstance(t, dict) and t.get("name")]


# {"Tên cột trên Lark": hàm(order) -> giá trị}. Trả None = bỏ qua cột đó (không xoá dữ liệu cũ).
LATE_FIELDS = {
    "Trạng thái":                    lambda o: STATUS_MAP.get(o.get("status")),
    "Người xử lý":                   lambda o: nested(o, "assigning_seller", "name"),
    "Nhân viên chăm sóc khách hàng": lambda o: nested(o, "assigning_care", "name"),
    "Thẻ Pancake":                   lambda o: conversation_tags(o),  # thẻ hội thoại của khách: C1, C2, C3, Zalo...
    "Ghi chú nội bộ":                lambda o: o.get("note"),
    "Ghi chú để in":                 lambda o: o.get("note_print"),
    "Người nhận":                    lambda o: o.get("bill_full_name"),
    "Số điện thoại":                 lambda o: o.get("bill_phone_number"),
    "Địa chỉ":                       lambda o: nested(o, "shipping_address", "full_address"),
    "Ngày cập nhật":                 lambda o: o.get("updated_at"),
    # "Ghi chú trao đổi": chưa rõ trường tương ứng trong JSON -> chưa đồng bộ
    # "Thẻ": chưa rõ (cột đang trống) -> chưa đồng bộ
}
DATE_COLS = {"Ngày cập nhật"}


def build_key(order):
    sid = order.get("system_id")
    return f"S{SHOP_ID}O{sid}" if sid is not None else None


def http(method, url, retries=4, **kw):
    for i in range(retries):
        try:
            r = requests.request(method, url, timeout=90, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            if r.status_code in (400, 401, 403, 404):
                sys.exit(f"HTTP {r.status_code} tại {url.split('?')[0]}\n{r.text[:600]}\n"
                         "-> Nếu là Lark 403: thêm app vào Base (Chia sẻ/... > Thêm ứng dụng tài liệu) với quyền Chỉnh sửa, "
                         "và bật scope bitable:app cho app rồi phát hành phiên bản.")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            # Chỉ in loại lỗi, không in nội dung lỗi (có thể chứa đường dẫn kèm API key)
            if i == retries - 1:
                sys.exit(f"Lỗi mạng sau {retries} lần thử ({type(e).__name__}) tại {url.split('?')[0]}")
            print(f"  retry ({type(e).__name__})")
            time.sleep(2 * (i + 1))
    sys.exit(f"Máy chủ quá tải sau {retries} lần thử tại {url.split('?')[0]}")


# ---------------- Pancake ----------------
def fetch_orders(start_dt, end_dt, by):
    orders, page = [], 1
    while True:
        params = {
            "api_key": PANCAKE_API_KEY,
            "page_size": PAGE_SIZE,
            "page_number": page,
            "startDateTime": int(start_dt.timestamp()),
            "endDateTime": int(end_dt.timestamp()),
            "updateStatus": by,
        }
        j = http("GET", f"{PANCAKE_BASE}/shops/{SHOP_ID}/orders", params=params).json()
        data = j.get("data") or []
        orders.extend(data)
        total_pages = j.get("total_pages")
        print(f"  Pancake trang {page}/{total_pages or '?'}: {len(data)} đơn")
        if not data or len(data) < PAGE_SIZE or (total_pages and page >= total_pages):
            break
        page += 1
        time.sleep(0.2)
    return orders


# ---------------- Lark ----------------
def lark_headers():
    j = http("POST", f"{LARK_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal",
             json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}).json()
    if j.get("code") != 0:
        sys.exit(f"Lỗi lấy token Lark (kiểm tra LARK_APP_ID / LARK_APP_SECRET): {j}")
    return {"Authorization": f"Bearer {j['tenant_access_token']}", "Content-Type": "application/json"}


def table_url(suffix):
    return f"{LARK_DOMAIN}/open-apis/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/{suffix}"


FIELD_INFO = {}


def ensure_options(headers, orders, cols, meta, dry):
    """Tự thêm option mới vào các cột chọn (trong AUTO_CREATE_OPTIONS) nếu Pancake có giá trị mà Lark chưa có."""
    for col in cols:
        if col not in AUTO_CREATE_OPTIONS or col not in meta:
            continue
        ftype, options = meta[col]
        if ftype not in (3, 4):
            continue
        needed = set()
        for o in orders:
            v = LATE_FIELDS[col](o)
            if isinstance(v, list):
                needed.update(x for x in v if x)
            elif v:
                needed.add(v)
        missing = sorted(needed - options)
        if not missing:
            continue
        print(f"  Cột '{col}': thêm {len(missing)} option mới: {missing}")
        if not dry:
            info = FIELD_INFO[col]
            new_opts = list(info["options"]) + [{"name": n, "color": i % 55} for i, n in enumerate(missing, len(info["options"]))]
            j = http("PUT", table_url(f"fields/{info['id']}"), headers=headers,
                     json={"field_name": col, "type": info["type"], "property": {"options": new_opts}}).json()
            if j.get("code") != 0:
                print(f"  [!] Không thêm được option cho '{col}': {j}")
                continue
        options.update(missing)  # coi như đã có để các bước sau không cảnh báo


def lark_fields(headers):
    """{tên cột: (type, set tên option nếu là cột chọn)}"""
    out, token = {}, None
    while True:
        params = {"page_size": 100}
        if token:
            params["page_token"] = token
        j = http("GET", table_url("fields"), headers=headers, params=params).json()
        if j.get("code") != 0:
            sys.exit(f"Lỗi đọc danh sách cột Lark: {j}")
        for it in j["data"]["items"]:
            raw_opts = (it.get("property") or {}).get("options") or []
            opts = {x["name"] for x in raw_opts} if it["type"] in (3, 4) else None
            out[it["field_name"]] = (it["type"], opts)
            FIELD_INFO[it["field_name"]] = {"id": it["field_id"], "type": it["type"], "options": raw_opts}
        if not j["data"].get("has_more"):
            break
        token = j["data"]["page_token"]
    return out


def flatten_text(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, dict):
        return flatten_text(v.get("value") if "value" in v else v.get("text"))
    if isinstance(v, list):
        return "".join(filter(None, (flatten_text(x) for x in v)))
    return str(v)


def lark_existing(headers):
    """{mã đơn: record_id} của toàn bộ bảng."""
    mapping, token = {}, None
    while True:
        params = {"page_size": 500}
        if token:
            params["page_token"] = token
        j = http("POST", table_url("records/search"), headers=headers, params=params,
                 json={"field_names": [KEY_FIELD]}).json()
        if j.get("code") != 0:
            sys.exit(f"Lỗi đọc bảng Lark: {j}")
        for it in j["data"].get("items") or []:
            k = flatten_text(it["fields"].get(KEY_FIELD))
            if k:
                mapping[k.strip()] = it["record_id"]
        if not j["data"].get("has_more"):
            break
        token = j["data"]["page_token"]
    return mapping


def to_dt(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v if v < 1e11 else v / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", ""))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


WARNED = set()


def warn_once(msg):
    if msg not in WARNED:
        WARNED.add(msg)
        print("  [!]", msg)


def convert(col, value, ftype, options):
    """Ép giá trị sang kiểu cột Lark. Trả (ghi_được, giá_trị)."""
    if ftype in (20, 19, 18, 21, 1001, 1002, 1003, 1004, 1005, 11, 17):
        warn_once(f"Cột '{col}' là cột công thức/tự động/người/file -> không ghi được, bỏ qua")
        return False, None
    if col in DATE_COLS or ftype == 5:
        dt = to_dt(value)
        if dt is None:
            return False, None
        if ftype == 5:
            return True, int(dt.timestamp() * 1000)
        return True, dt.astimezone(VN_TZ).strftime(TEXT_DATE_FMT)
    if ftype == 2:
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, None
    if ftype in (3, 4):
        names = value if isinstance(value, list) else ([value] if value not in (None, "") else [])
        bad = [n for n in names if options is not None and n not in options]
        for n in bad:
            warn_once(f"Cột '{col}' chưa có option '{n}' trên Lark -> bỏ qua (thêm option hoặc sửa mapping)")
        names = [n for n in names if n not in bad]
        if ftype == 4:
            return True, names
        return (True, names[0]) if names else (False, None)
    if isinstance(value, list):
        return True, ", ".join(map(str, value))
    return True, "" if value is None else str(value)


def build_fields(order, fields_meta, cols):
    out = {}
    for col in cols:
        getter = LATE_FIELDS[col]
        if col not in fields_meta:
            warn_once(f"Lark không có cột '{col}' -> bỏ qua")
            continue
        raw = getter(order)
        if raw is None:
            continue
        ftype, options = fields_meta[col]
        ok, val = convert(col, raw, ftype, options)
        if ok:
            out[col] = val
    return out


def lark_batch(headers, action, records, dry):
    global LOI_GHI
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        if dry:
            print(f"  [dry-run] {action}: {len(chunk)} dòng")
            continue
        j = http("POST", table_url(f"records/{action}"), headers=headers, json={"records": chunk}).json()
        if j.get("code") == 0:
            print(f"  {action}: {len(chunk)} dòng -> OK")
        else:
            LOI_GHI += 1
            print(f"  {action}: {len(chunk)} dòng -> LỖI code={j.get('code')} msg={j.get('msg')}")
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"quét lùi N ngày (mặc định {DEFAULT_DAYS})")
    ap.add_argument("--by", choices=["inserted_at", "updated_at"], default=UPDATE_STATUS,
                    help="lọc theo ngày tạo (inserted_at) hay ngày cập nhật (updated_at)")
    ap.add_argument("--fields", default=",".join(DEFAULT_FIELDS),
                    help='các cột cần cập nhật, cách nhau bằng dấu phẩy, hoặc "all"')
    ap.add_argument("--from", dest="d_from", help="YYYY-MM-DD")
    ap.add_argument("--to", dest="d_to", help="YYYY-MM-DD")
    ap.add_argument("--create-missing", action="store_true", help="tạo dòng mới nếu đơn chưa có trên Lark")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dump-sample", action="store_true", help="in JSON 1 đơn rồi thoát (chỉ dùng trên máy)")
    a = ap.parse_args()

    thieu = [k for k, v in {f"PANCAKE_API_KEY_{SHOP_ID}": PANCAKE_API_KEY, "LARK_APP_ID": LARK_APP_ID,
                            "LARK_APP_SECRET": LARK_APP_SECRET}.items() if not v]
    if thieu:
        sys.exit(f"Thiếu biến môi trường: {', '.join(thieu)} (kiểm tra secret và phần env trong file yml).")

    # Tính ngày theo giờ Việt Nam (máy chủ GitHub chạy giờ UTC)
    now_vn = datetime.now(VN_TZ)
    if a.d_from:
        start = datetime.strptime(a.d_from, "%Y-%m-%d").replace(tzinfo=VN_TZ)
        end = (datetime.strptime(a.d_to, "%Y-%m-%d").replace(tzinfo=VN_TZ) if a.d_to else now_vn) + timedelta(days=1)
    else:
        start = now_vn - timedelta(days=a.days)
        end = now_vn + timedelta(days=1)

    cols = list(LATE_FIELDS) if a.fields.strip() == "all" else [c.strip() for c in a.fields.split(",") if c.strip()]
    unknown = [c for c in cols if c not in LATE_FIELDS]
    if unknown:
        sys.exit(f"Cột không có trong LATE_FIELDS: {unknown}. Có: {list(LATE_FIELDS)}")

    print(f"[{now_vn:%F %T} giờ VN] Lấy đơn theo {a.by} từ {start:%F} đến {end:%F} | cột: {cols}")
    orders = fetch_orders(start, end, a.by)
    print(f"Tổng {len(orders)} đơn từ Pancake")

    if a.dump_sample:
        if os.getenv("GITHUB_ACTIONS"):
            sys.exit("--dump-sample có thông tin khách hàng, chỉ chạy trên máy, không chạy trên GitHub (log public).")
        import json
        if orders:
            print(json.dumps(orders[0], ensure_ascii=False, indent=2))
        return

    headers = lark_headers()
    meta = lark_fields(headers)
    if KEY_FIELD not in meta:
        sys.exit(f"Không thấy cột khoá '{KEY_FIELD}' trên Lark. Cột hiện có: {list(meta)}")
    existing = lark_existing(headers)
    print(f"Lark hiện có {len(existing)} dòng")
    ensure_options(headers, orders, cols, meta, a.dry_run)

    updates, creates, skipped = [], [], 0
    for o in orders:
        key = build_key(o)
        if not key:
            skipped += 1
            continue
        fields = build_fields(o, meta, cols)
        if key in existing:
            if fields:
                updates.append({"record_id": existing[key], "fields": fields})
        elif a.create_missing:
            fields[KEY_FIELD] = key
            creates.append({"fields": fields})
        else:
            skipped += 1

    print(f"Cập nhật {len(updates)} | Tạo mới {len(creates)} | Bỏ qua (chưa có trên Lark) {skipped}")
    if updates:
        lark_batch(headers, "batch_update", updates, a.dry_run)
    if creates:
        lark_batch(headers, "batch_create", creates, a.dry_run)
    if LOI_GHI:
        sys.exit(f"Có {LOI_GHI} lô ghi Lark bị lỗi.")
    print("Xong.")


if __name__ == "__main__":
    main()