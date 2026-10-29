"""
ĐỒNG BỘ ĐƠN ĐÃ GIAO THÀNH CÔNG: PANCAKE -> LARK (bảng Kế toán 2026)

- Quét đơn CẬP NHẬT từ ngày TU_NGAY (mặc định 01/10/2026). Để TU_NGAY = "" thì quay lại quét SO_NGAY_QUET ngày gần nhất
- Chỉ lấy đơn CÓ ngày giao hàng thành công (>= TU_NGAY) = lúc đơn chuyển sang "Đã nhận" trên Pancake
- Đơn đã có trên Lark (trùng ID) thì BỎ QUA, không thêm, không sửa
- Số dòng của 1 đơn = số nhóm hàng hóa. "Tên hàng" = tên hàng hóa phân loại theo Mã mẫu mã (giống công thức
  cột "Tên hàng hóa" trên bảng POS). Trong 1 đơn, các sản phẩm CÙNG tên hàng + CÙNG đơn giá được gộp thành 1 dòng:
  tên hàng, tổng số lượng, đơn giá, thành tiền, thuế; cột "Mã hàng" ghi tất cả mã của nhóm, ngăn cách bằng ";" + xuống dòng.
- ID ghi ở MỌI dòng của đơn. Thông tin chung còn lại (ngày giao, khách, MST, nguồn, kho) và Tổng hóa đơn
  chỉ ghi ở DÒNG ĐẦU của đơn.
- Bỏ hàng có đơn giá = 0.
- MST bỏ hết khoảng trắng. SĐT giữ nguyên như trên POS"""
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

# Đọc từ biến môi trường (GitHub Secrets) — KHÔNG hardcode trong code
# Cần 5 secret: PANCAKE_API_KEY, PANCAKE_SHOP_ID, LARK_APP_ID, LARK_APP_SECRET, LARK_BASE_APP_TOKEN_KE_TOAN
PANCAKE_API_KEY = os.getenv("PANCAKE_API_KEY", "").strip()
PANCAKE_SHOP_ID = os.getenv("PANCAKE_SHOP_ID", "").strip()

# ================== CẤU HÌNH ==================
TU_NGAY = "2026-10-01"     # lấy đơn giao thành công từ ngày này (YYYY-MM-DD, giờ VN). "" = dùng SO_NGAY_QUET
SO_NGAY_QUET = 3           # chỉ dùng khi TU_NGAY = "": quét đơn cập nhật trong N ngày gần nhất
LOC_THE_VAT = True         # True = chỉ lấy đơn có thẻ VAT
VAT_TAG_NAMES = ["VAT", "VAT cá nhân", "VAT công ty"]
THUE_SUAT = 0.08           # dùng khi sản phẩm không có thuế suất riêng trên POS
GIA_DA_GOM_VAT = False     # False = giá trên POS là giá chưa VAT, dùng luôn làm đơn giá (không tách VAT)
DELIVERED_STATUS = 3       # trạng thái "Đã nhận" (giao thành công)


DRY_RUN = False            # True = chỉ in ra, không ghi Lark

PAGE_SIZE = 500
SO_TRANG_SONG_SONG = 4

LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN_KE_TOAN", "").strip()   # base Kế toán, KHÁC base Sale
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID_KE_TOAN", "").strip() or "tblsfYXNulnB8aen"
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

# Phân loại "Tên hàng" theo Mã mẫu mã (giống công thức IFS cột "Tên hàng hóa" trên bảng POS).
# Xét từ trên xuống, gặp từ khoá đầu tiên có trong Mã mẫu mã (không phân biệt hoa thường) thì lấy tên đó.
# Không khớp từ khoá nào -> lấy Mã sản phẩm. Thêm / sửa loại hàng: sửa danh sách này.
QUY_TAC_TEN_HANG = [
    (["mũ"], "Mũ đồng phục"),
    (["nón"], "Nón đồng phục"),
    (["snapfit workwear"], "Tạp dề chống nước có cúc"),
    (["aquatex", "chống nước"], "Tạp dề chống nước"),
    (["đeo vai", "canvastique", "dáng dài"], "Tạp dề dài"),
    (["dáng ngắn"], "Tạp dề ngắn"),
    (["sơ mi"], "Áo sơ mi đồng phục"),
    (["phông", "oversize", "thun"], "Áo phông đồng phục"),
    (["polo", "pique"], "Áo polo đồng phục"),
    (["bếp"], "Áo bếp đồng phục"),
    (["hoodie"], "Áo"),
    (["đồng phục", "uniform", "dongphucdeme"], "Áo đồng phục"),
]

VN_TZ = timezone(timedelta(hours=7))
LOAI_GHI_DUOC = {1, 2, 3, 4, 5, 13}   # văn bản, số, chọn 1, chọn nhiều, ngày, SĐT


def norm(s):
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


VAT_TAGS = {norm(x) for x in VAT_TAG_NAMES}


def moc_bat_dau():
    """Thời điểm bắt đầu quét (datetime giờ VN)."""
    if TU_NGAY.strip():
        return datetime.strptime(TU_NGAY.strip(), "%Y-%m-%d").replace(tzinfo=VN_TZ)
    return datetime.now(VN_TZ) - timedelta(days=SO_NGAY_QUET)


def ten_hang_hoa(ma_mau_ma: str, ma_san_pham: str) -> str:
    m = unicodedata.normalize("NFC", str(ma_mau_ma or "")).lower()
    for tu_khoa, ten in QUY_TAC_TEN_HANG:
        if any(k in m for k in tu_khoa):
            return ten
    return str(ma_san_pham or "")


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
            print(f"  trang {page}: lỗi mạng {type(e).__name__}, thử lại...")
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
    tu = datetime.fromtimestamp(start_ts, VN_TZ).strftime("%d/%m/%Y")
    print(f"-> Tải {len(orders)} đơn cập nhật từ {tu} ({total} trang)")
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


def tao_dong(orders, tu_dt):
    rows = []
    cac_don = []   # [(ngày giao, các dòng của đơn)] để sắp xếp trước khi ghi
    for order in orders:
        ngay_giao = ngay_giao_thanh_cong(order)
        if not ngay_giao: continue                       # chỉ đơn có ngày giao thành công
        if ngay_giao < tu_dt: continue                   # chỉ đơn giao thành công từ TU_NGAY trở đi
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
        # 1. Tính từng sản phẩm
        items = []
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
            if don_gia <= 0: continue                    # bỏ hàng có đơn giá = 0 (quà tặng...)
            ma_mau_ma = vi.get("display_id") or it.get("display_id") or ""
            ma_san_pham = (vi.get("product_display_id") or it.get("product_display_id")
                           or (it.get("product") or {}).get("display_id") or vi.get("name") or it.get("name") or "")
            items.append({"ma_hang": ma_mau_ma or ma_san_pham, "qty": qty, "don_gia": don_gia, "thue": thue,
                          "ten_hang": ten_hang_hoa(ma_mau_ma, ma_san_pham)})

        # 2. Gộp theo (tên hàng, đơn giá, thuế suất), giữ thứ tự xuất hiện; mỗi nhóm các dòng nằm liền nhau
        nhom = {}
        for x in items:
            nhom.setdefault((x["ten_hang"], x["don_gia"], x["thue"]), []).append(x)

        dong_don = []     # các dòng của đơn này
        tong_don = 0      # tổng hóa đơn của cả đơn (thành tiền + tiền thuế của mọi nhóm)
        for (ten, don_gia, thue), ds in nhom.items():
            so_luong = sum(x["qty"] for x in ds)
            thanh_tien = so_luong * don_gia
            tien_thue = round(thanh_tien * thue)
            tong_don += thanh_tien + tien_thue
            # 1 nhóm hàng hóa = 1 dòng; các mã hàng trong nhóm nối bằng ";" + xuống dòng
            ma_hang = ";\n".join(x["ma_hang"] for x in ds)
            dong_don.append({"_id": base["id"], "id": base["id"], "ma_hang": ma_hang, "ten_hang": ten,
                             "so_luong": so_luong, "don_gia": don_gia, "thanh_tien": thanh_tien,
                             "thue_suat": thue, "tien_thue": tien_thue})
        if dong_don:
            # ID ghi ở mọi dòng; thông tin chung còn lại (ngày giao, khách, MST, nguồn, kho...) + Tổng hóa đơn: chỉ ở dòng đầu đơn
            dong_don[0].update(base)
            dong_don[0]["tong"] = tong_don
            cac_don.append((ngay_giao, dong_don))

    # Sắp xếp theo ngày giao thành công từ cũ nhất đến mới nhất (các dòng trong 1 đơn giữ nguyên thứ tự)
    cac_don.sort(key=lambda d: d[0])
    for _, dong_don in cac_don:
        rows.extend(dong_don)
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
    """Thêm records. Trả về list record_id đúng thứ tự đầu vào (None nếu dòng đó bị lỗi)."""
    url = f"{LARK}/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/batch_create"
    ids = []
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        kq = [None] * len(chunk)
        for attempt in range(3):
            try:
                r = requests.post(url, headers=h, json={"records": chunk}, timeout=60).json()
                if r.get("code") == 0:
                    tao = (r.get("data") or {}).get("records") or []
                    for k, rec in enumerate(tao[:len(chunk)]):
                        kq[k] = rec.get("record_id")
                else:
                    print(f"   !! Lỗi thêm: {r.get('code')} {r.get('msg')}")
                break
            except requests.exceptions.RequestException:
                time.sleep(5)
        ids.extend(kq)
    return ids


# ===================== MAIN =====================
def main():
    t0 = time.time()
    thieu = [k for k, v in {"PANCAKE_API_KEY": PANCAKE_API_KEY, "PANCAKE_SHOP_ID": PANCAKE_SHOP_ID,
                            "LARK_APP_ID": LARK_APP_ID, "LARK_APP_SECRET": LARK_APP_SECRET,
                            "LARK_BASE_APP_TOKEN_KE_TOAN": LARK_APP_TOKEN}.items() if not v]
    if thieu:
        raise SystemExit(f"🛑 Thiếu biến môi trường: {', '.join(thieu)} (kiểm tra secret và phần env trong file yml).")
    h = lark_headers()
    types = loai_cot(h)
    if COT["id"] not in types:
        raise SystemExit("!! Bảng Lark không có cột ID")
    thieu = [c for c in COT.values() if c not in types]
    if thieu: print(f"  Bảng Lark không có cột: {', '.join(thieu)} (bỏ qua)")
    ghi = {k: (c, types[c]) for k, c in COT.items() if c in types and types[c] in LOAI_GHI_DUOC}

    tu_dt = moc_bat_dau()
    start_ts = int(tu_dt.timestamp())
    end_ts = int(time.time())
    orders = tai_don(start_ts, end_ts)
    rows = tao_dong(orders, tu_dt)
    ids = {r["_id"] for r in rows}
    print(f"-> {len(ids)} đơn có ngày giao thành công từ {tu_dt:%d/%m/%Y} ({len(rows)} dòng sản phẩm)")
    if not rows:
        print("Không có đơn mới."); return

    da_co = id_da_co(h, ids)
    moi = [r for r in rows if r["_id"] not in da_co]
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
        print(f"[DRY_RUN] Chưa ghi: {len(records)} dòng.")
        # Mẫu dòng có tên, SĐT, MST khách -> chỉ in khi chạy trên máy, không in lên log GitHub (repo public)
        if not os.getenv("GITHUB_ACTIONS"):
            print(json.dumps(records[:2], ensure_ascii=False, indent=2))
        return
    ids_ghi = them_moi(h, records)
    ok = sum(1 for x in ids_ghi if x)
    print(f"✅ Đã thêm {ok}/{len(records)} dòng (mất {time.time() - t0:.0f} giây)")


if __name__ == "__main__":
    main()