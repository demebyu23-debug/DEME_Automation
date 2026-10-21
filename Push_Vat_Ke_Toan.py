"""
ĐỒNG BỘ ĐƠN ĐÃ GIAO THÀNH CÔNG: PANCAKE -> LARK (bảng Kế toán 2026)

- Chỉ quét đơn CẬP NHẬT trong 3 ngày gần nhất (không dùng cache, không quét lại cả tháng)
- Chỉ lấy đơn CÓ ngày giao hàng thành công = lúc đơn chuyển sang "Đã nhận" trên Pancake
- Đơn đã có trên Lark (trùng ID) thì BỎ QUA, không thêm, không sửa
- Mỗi sản phẩm 1 dòng. MST bỏ hết khoảng trắng. SĐT giữ nguyên như trên POS
"""
import os
import time
import json
import unicodedata
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

PANCAKE_API_KEY = os.getenv("PANCAKE_API_KEY")
PANCAKE_SHOP_ID = "1320043"

# ================== CẤU HÌNH ==================
SO_NGAY_QUET = 3           # quét đơn cập nhật trong N ngày gần nhất
LOC_THE_VAT = True         # True = chỉ lấy đơn có thẻ VAT
VAT_TAG_NAMES = ["VAT", "VAT cá nhân", "VAT công ty"]
THUE_SUAT = 0.08           # dùng khi sản phẩm không có thuế suất riêng trên POS
GIA_DA_GOM_VAT = True      # giá trên POS đã gồm VAT -> tách ra giá trước VAT
DELIVERED_STATUS = 3       # trạng thái "Đã nhận" (giao thành công)


DRY_RUN = False            # True = chỉ in ra, không ghi Lark

PAGE_SIZE = 500
SO_TRANG_SONG_SONG = 4

LARK_APP_ID = "cli_aa817039ac4bded3"
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET")
LARK_APP_TOKEN = "Cr25bE0NXaUl99s6oaBl4qbqgOg"
LARK_TABLE_ID = "tblsfYXNulnB8aen"
LARK = "https://open.larksuite.com/open-apis"

# Tên cột trên Lark (đúng như bảng Kế toán 2026)
COT = {
    "id": "ID",
    "ngay_giao": "Ngày giao hàng thành công",
    "ma_kh": "Mã khách hàng",
    "nhan_vien": "Mã nhân viên bán hàng",
    "ten_kh": "Tên khách hàng",
    "dia_chi_kh": "Địa chỉ KH",
    "mst": "MST",
    "ten_ct": "Tên công ty",
    "dia_chi_ct": "Địa chỉ",
    "ma_hang": "Mã hàng",
    "ten_hang": "Tên hàng",
    "so_luong": "Số lượng",
    "don_gia": "Đơn giá",
    "thanh_tien": "Thành tiền trước VAT",
    "thue_suat": "Thuế suất",
    "tien_thue": "Tiền thuế",
    "tong": "Tổng hóa đơn",
    "nguon": "Nguồn đơn hàng",
    "kho": "Kho xuất hàng",
}
# ==============================================

VN_TZ = timezone(timedelta(hours=7))
LOAI_GHI_DUOC = {1, 2, 3, 4, 5, 13}   # văn bản, số, chọn 1, chọn nhiều, ngày, SĐT


def norm(s):
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


VAT_TAGS = {norm(x) for x in VAT_TAG_NAMES}


# ===================== PANCAKE =====================
def to_dt(val):
    if not val: return None
    try:
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val / 1000 if val > 1e11 else val, VN_TZ)
        return datetime.strptime(str(val).replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S") \
            .replace(tzinfo=timezone.utc).astimezone(VN_TZ)
    except Exception:
        return None


def ngay_giao_thanh_cong(order):
    """Ngày giao thành công = lúc đơn chuyển sang 'Đã nhận' trên Pancake (lấy lần gần nhất).
    Đơn chưa từng chuyển sang 'Đã nhận' -> None (không lấy)."""
    moc = None
    for h in order.get("status_history") or []:
        if str(h.get("status")) == str(DELIVERED_STATUS):
            moc = to_dt(h.get("updated_at") or h.get("created_at"))
    return moc


def tai_trang(page, start_ts, end_ts):
    url = f"https://pos.pages.fm/api/v1/shops/{PANCAKE_SHOP_ID}/orders"
    params = {"api_key": PANCAKE_API_KEY, "page_number": page, "page_size": PAGE_SIZE,
              "status": "-1", "updateStatus": "updated_at",
              "startDateTime": start_ts, "endDateTime": end_ts}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code == 200:
                return r.json()
            print(f"  trang {page}: HTTP {r.status_code}, thử lại...")
        except requests.exceptions.RequestException as e:
            print(f"  trang {page}: lỗi mạng {e}, thử lại...")
        time.sleep(3 * (attempt + 1))
    print(f"  !! Bỏ qua trang {page} sau 3 lần lỗi")
    return {}


def tai_don(start_ts, end_ts):
    """Tải trang 1 để biết tổng số trang, rồi tải các trang còn lại song song."""
    first = tai_trang(1, start_ts, end_ts)
    orders = first.get("data") or []
    total = int(first.get("total_pages") or 1)
    if total > 1:
        with ThreadPoolExecutor(max_workers=SO_TRANG_SONG_SONG) as ex:
            for body in ex.map(lambda p: tai_trang(p, start_ts, end_ts), range(2, total + 1)):
                orders.extend(body.get("data") or [])
    print(f"-> Tải {len(orders)} đơn cập nhật trong {SO_NGAY_QUET} ngày ({total} trang)")
    return orders


def chuan_mst(mst):
    """Bỏ mọi khoảng trắng (kể cả dấu cách đặc biệt) trong MST."""
    return "".join(str(mst or "").split())


def nguon_don(order):
    platform = order.get("order_sources_name") or ""
    if platform.lower().startswith("tiktokshop"): return platform
    channel = order.get("account_name") or (order.get("page") or {}).get("name") or ""
    if platform and channel: return f"{platform} / {channel}"
    return channel or platform


def tao_dong(orders):
    rows = []
    for order in orders:
        ngay_giao = ngay_giao_thanh_cong(order)
        if not ngay_giao: continue                       # chỉ đơn có ngày giao thành công
        if LOC_THE_VAT:
            tags = {norm(t.get("name")) for t in (order.get("tags") or []) if isinstance(t, dict)}
            if not (tags & VAT_TAGS): continue

        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        company = customer.get("company_info") or {}
        phones = customer.get("phone_numbers") or []
        base = {
            "id": str(order.get("system_id") or order.get("id") or ""),
            "ngay_giao": ngay_giao,
            "ma_kh": str(shipping.get("phone_number") or (phones[0] if phones else "")).strip(),
            "nhan_vien": (order.get("assigning_seller") or {}).get("name") or "",
            "ten_kh": shipping.get("full_name") or customer.get("name") or "",
            "dia_chi_kh": shipping.get("full_address") or "",
            "mst": chuan_mst(company.get("company_tax_id")),
            "ten_ct": company.get("company_name") or "",
            "dia_chi_ct": company.get("company_address") or "",
            "nguon": nguon_don(order),
            "kho": (order.get("warehouse_info") or {}).get("name") or "",
        }
        for it in order.get("items") or []:
            qty = int(it.get("quantity") or 0)
            if qty <= 0: continue
            vi = it.get("variation_info") or {}
            gia = float(vi.get("retail_price") or it.get("retail_price") or 0)
            giam = float(it.get("total_discount") or 0)
            if not giam and it.get("discount_each_product"):
                giam = float(it.get("discount_each_product")) * qty
            thue = vi.get("tax_rate")
            thue = float(thue) if thue not in (None, "") else THUE_SUAT   # thuế suất riêng của sản phẩm
            don_gia = (gia * qty - giam) / qty
            if GIA_DA_GOM_VAT: don_gia /= (1 + thue)
            don_gia = round(don_gia)
            thanh_tien = qty * don_gia
            tien_thue = round(thanh_tien * thue)
            rows.append({**base,
                         "ma_hang": vi.get("display_id") or it.get("product_display_id") or "",
                         "ten_hang": vi.get("name") or it.get("name") or "",
                         "so_luong": qty, "don_gia": don_gia, "thanh_tien": thanh_tien,
                         "thue_suat": thue, "tien_thue": tien_thue, "tong": thanh_tien + tien_thue})
    return rows


# ===================== LARK =====================
def lark_headers():
    r = requests.post(f"{LARK}/auth/v3/tenant_access_token/internal",
                      json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET}, timeout=30).json()
    if r.get("code") != 0:
        raise SystemExit(f"!! Lỗi token Lark: {r}. Kiểm tra LARK_APP_SECRET trong .env")
    return {"Authorization": f"Bearer {r['tenant_access_token']}", "Content-Type": "application/json; charset=utf-8"}


def loai_cot(h):
    """{tên cột: loại} - 1 lần gọi, để biết cột nào là ngày, số, công thức."""
    r = requests.get(f"{LARK}/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/fields",
                     headers=h, params={"page_size": 100}, timeout=30).json()
    if r.get("code") != 0:
        raise SystemExit(f"!! Không đọc được bảng Lark: {r.get('msg')} -> kiểm tra app đã được thêm vào Base chưa")
    return {f["field_name"]: f.get("type") for f in (r.get("data") or {}).get("items") or []}


def id_da_co(h, ids):
    """Chỉ tra các ID vừa quét được (không đọc cả bảng). Trả về tập ID đã có trên Lark."""
    url = f"{LARK}/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/search"
    co = set()
    ids = list(ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        body = {"field_names": [COT["id"]],
                "filter": {"conjunction": "or",
                           "conditions": [{"field_name": COT["id"], "operator": "is", "value": [x]} for x in chunk]}}
        page_token = ""
        while True:
            params = {"page_size": 500}
            if page_token: params["page_token"] = page_token
            r = requests.post(url, headers=h, params=params, json=body, timeout=60).json()
            if r.get("code") != 0:
                raise SystemExit(f"!! Lỗi tra ID trên Lark: {r.get('code')} {r.get('msg')}")
            d = r.get("data") or {}
            for it in d.get("items") or []:
                v = (it.get("fields") or {}).get(COT["id"])
                if isinstance(v, list): v = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
                co.add(str(v).strip().removesuffix(".0"))
            if not d.get("has_more"): break
            page_token = d.get("page_token")
    return co


def gia_tri(v, loai):
    if v is None or v == "": return None
    if loai == 5: return int(v.timestamp() * 1000) if isinstance(v, datetime) else None
    if isinstance(v, datetime): v = v.strftime("%Y-%m-%d %H:%M:%S")
    if loai == 2:
        try: return float(v)
        except (TypeError, ValueError): return None
    if loai == 4: return [str(v)]
    return str(v)


def them_moi(h, records):
    url = f"{LARK}/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_create"
    ok = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        for attempt in range(3):
            try:
                r = requests.post(url, headers=h, json={"records": chunk}, timeout=60).json()
                if r.get("code") == 0: ok += len(chunk)
                else: print(f"   !! Lỗi thêm: {r.get('code')} {r.get('msg')}")
                break
            except requests.exceptions.RequestException:
                time.sleep(5)
    return ok


# ===================== MAIN =====================
def main():
    t0 = time.time()
    if not LARK_APP_SECRET:
        raise SystemExit("!! Thiếu LARK_APP_SECRET trong file .env")
    h = lark_headers()
    types = loai_cot(h)
    if COT["id"] not in types:
        raise SystemExit("!! Bảng Lark không có cột ID")
    thieu = [c for c in COT.values() if c not in types]
    if thieu: print(f"  Bảng Lark không có cột: {', '.join(thieu)} (bỏ qua)")
    ghi = {k: (c, types[c]) for k, c in COT.items() if c in types and types[c] in LOAI_GHI_DUOC}

    end_ts = int(time.time())
    orders = tai_don(end_ts - SO_NGAY_QUET * 86400, end_ts)
    rows = tao_dong(orders)
    ids = {r["id"] for r in rows}
    print(f"-> {len(ids)} đơn có ngày giao thành công ({len(rows)} dòng sản phẩm)")
    if not rows:
        print("Không có đơn mới."); return

    da_co = id_da_co(h, ids)
    moi = [r for r in rows if r["id"] not in da_co]
    print(f"-> Đã có trên Lark: {len(ids & da_co)} đơn (bỏ qua) | Đơn mới: {len(ids - da_co)} đơn, {len(moi)} dòng")
    if not moi:
        print(f"Không có đơn mới cần thêm (mất {time.time() - t0:.0f} giây)."); return

    records = []
    for r in moi:
        f = {}
        for key, (ten, loai) in ghi.items():
            v = gia_tri(r.get(key), loai)
            if v is not None: f[ten] = v
        records.append({"fields": f})

    if DRY_RUN:
        print("[DRY_RUN] Chưa ghi. Mẫu 1 dòng:")
        print(json.dumps(records[:1], ensure_ascii=False, indent=2))
        return
    ok = them_moi(h, records)
    print(f"✅ Đã thêm {ok}/{len(records)} dòng (mất {time.time() - t0:.0f} giây)")


if __name__ == "__main__":
    main()