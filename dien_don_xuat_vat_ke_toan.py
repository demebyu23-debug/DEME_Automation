"""
ĐỒNG BỘ ĐƠN ĐÃ GIAO THÀNH CÔNG: PANCAKE -> LARK (bảng Kế toán 2026)

- Quét đơn CẬP NHẬT từ ngày TU_NGAY (mặc định 01/10/2026). Để TU_NGAY = "" thì quay lại quét SO_NGAY_QUET ngày gần nhất
- Chỉ lấy đơn CÓ ngày giao hàng thành công (>= TU_NGAY) = lúc đơn chuyển sang "Đã nhận" trên Pancake
- Đơn đã có trên Lark (trùng ID): không sửa dòng cũ, chỉ BỔ SUNG nhóm hàng còn thiếu và dòng "Sản phẩm tặng" (nếu có giảm giá)
- Số dòng của 1 đơn = số nhóm hàng hóa. "Tên hàng" = tên hàng hóa phân loại theo Mã mẫu mã (giống công thức
  cột "Tên hàng hóa" trên bảng POS). Trong 1 đơn, các sản phẩm CÙNG tên hàng + CÙNG đơn giá được gộp thành 1 dòng:
  tên hàng, tổng số lượng, đơn giá, thành tiền, thuế; cột "Mã hàng" ghi tất cả mã của nhóm, ngăn cách bằng ";" + xuống dòng.
- ID ghi ở MỌI dòng của đơn. Thông tin chung còn lại (ngày giao, khách, MST, nguồn, kho) và Tổng hóa đơn
  chỉ ghi ở DÒNG ĐẦU của đơn.
- Bỏ hàng có đơn giá = 0, TRỪ sản phẩm có Mã sản phẩm là "Cờ" (đơn giá = 0 vẫn lấy).
- Đơn có giảm giá: thêm 1 dòng CUỐI đơn, Tên hàng = "Sản phẩm tặng", Số lượng = 1,
  Đơn giá / Thành tiền / Tiền thuế = giá trị giảm giá (SỐ ÂM).
- Đơn CHỈ có thẻ "VAT cá nhân" (không có "VAT công ty"): KHÔNG xuất thông tin công ty (MST, Tên công ty, Địa chỉ công ty).
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
TU_NGAY = ""     # lấy đơn giao thành công từ ngày này (YYYY-MM-DD, giờ VN). "" = dùng SO_NGAY_QUET
SO_NGAY_QUET = 3           # chỉ dùng khi TU_NGAY = "": quét đơn cập nhật trong N ngày gần nhất
LOC_THE_VAT = True         # True = chỉ lấy đơn có thẻ VAT
VAT_TAG_NAMES = ["VAT", "VAT cá nhân", "VAT công ty"]
TAG_VAT_CA_NHAN = "VAT cá nhân"
TAG_VAT_CONG_TY = "VAT công ty"
THUE_SUAT = 0.08           # dùng khi sản phẩm không có thuế suất riêng trên POS
GIA_DA_GOM_VAT = False     # False = giá trên POS là giá chưa VAT, dùng luôn làm đơn giá (không tách VAT)
DELIVERED_STATUS = 3       # trạng thái "Đã nhận" (giao thành công)

# Sản phẩm được giữ lại dù đơn giá = 0 (so khớp theo Mã sản phẩm, không phân biệt hoa thường)
MA_SP_GIU_DU_GIA_0 = ["Cờ"]
# True = so khớp CHỨA từ khóa (vd "Cờ đỏ", "Cờ VN" cũng tính); False = phải khớp chính xác
GIU_THEO_CHUA = False

# Dòng giảm giá ở cuối đơn
TEN_HANG_GIAM_GIA = "Sản phẩm tặng"
# False = đơn giá hàng hóa là GIÁ GỐC, giảm giá chỉ nằm ở dòng âm "Sản phẩm tặng" (tổng hóa đơn đúng, không trừ 2 lần)
# True  = đơn giá hàng hóa đã TRỪ giảm giá như logic cũ (nếu vẫn thêm dòng âm thì giảm giá bị trừ 2 lần)
GIA_TRU_GIAM_GIA = False

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
    (["áo gile"], "Áo gile đồng phục"),
    (["mũ lưỡi trai", "mũ nửa đầu"], "Nón đồng phục"),
    (["tạp dề ngắn xẻ giữa"], "Tạp dề ngắn xẻ giữa"),
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
NORM_VAT_CA_NHAN = norm(TAG_VAT_CA_NHAN)
NORM_VAT_CONG_TY = norm(TAG_VAT_CONG_TY)
GIU_GIA_0 = {norm(x) for x in MA_SP_GIU_DU_GIA_0}
TEN_GIAM_GIA_NORM = norm(TEN_HANG_GIAM_GIA)


def duoc_giu_du_gia_0(ma_san_pham):
    """True nếu sản phẩm (vd Cờ) vẫn được tính dù đơn giá = 0."""
    m = norm(ma_san_pham)
    if not m: return False
    if GIU_THEO_CHUA:
        return any(k in m for k in GIU_GIA_0)
    return m in GIU_GIA_0


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
    so_ca_nhan = 0
    for order in orders:
        ngay_giao = ngay_giao_thanh_cong(order)
        if not ngay_giao: continue                       # chỉ đơn có ngày giao thành công
        if ngay_giao < tu_dt: continue                   # chỉ đơn giao thành công từ TU_NGAY trở đi
        tags = {norm(t.get("name")) for t in (order.get("tags") or []) if isinstance(t, dict)}
        if LOC_THE_VAT and not (tags & VAT_TAGS): continue

        # Đơn CHỈ có "VAT cá nhân" (không có "VAT công ty") -> không xuất thông tin công ty
        chi_ca_nhan = (NORM_VAT_CA_NHAN in tags) and (NORM_VAT_CONG_TY not in tags)

        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        company = {} if chi_ca_nhan else (customer.get("company_info") or {})
        if chi_ca_nhan and (customer.get("company_info") or {}).get("company_name"):
            so_ca_nhan += 1                              # đếm đơn bị bỏ thông tin công ty (để in log)
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
            he_so = (1 + thue) if GIA_DA_GOM_VAT else 1
            gia_goc = round(gia / he_so)                                   # giá gốc (chưa trừ giảm giá)
            gia_net = round((gia * qty - giam) / qty / he_so)              # giá đã trừ giảm giá (logic cũ)
            don_gia = gia_net if GIA_TRU_GIAM_GIA else gia_goc

            ma_mau_ma = vi.get("display_id") or it.get("display_id") or ""
            ma_san_pham = (vi.get("product_display_id") or it.get("product_display_id")
                           or (it.get("product") or {}).get("display_id") or vi.get("name") or it.get("name") or "")

            # bỏ hàng có đơn giá = 0 (quà tặng...), TRỪ sản phẩm Cờ thì vẫn lấy
            if don_gia <= 0 and not duoc_giu_du_gia_0(ma_san_pham): continue
            if don_gia < 0: don_gia = 0                  # đề phòng giá âm

            items.append({"ma_hang": ma_mau_ma or ma_san_pham, "qty": qty, "don_gia": don_gia, "thue": thue,
                          "giam": giam / he_so, "gia_goc": gia_goc, "gia_net": gia_net,
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
                             "thue_suat": thue, "tien_thue": tien_thue,
                             # giá được coi là "đã có": giá gốc hoặc giá đã trừ giảm giá (dòng cũ trên Lark)
                             "_gia_khop": {don_gia} | {x["gia_net"] for x in ds}})

        # 3. Đơn có giảm giá -> thêm 1 dòng CUỐI đơn: "Sản phẩm tặng", SL 1, tiền = -(tổng giảm giá)
        #    Giảm giá của đơn nằm ở order["total_discount"] (gồm giảm giá cấp đơn + giảm giá từng sản phẩm).
        giam_items = sum(x["giam"] for x in items)                   # giảm giá gắn vào từng sản phẩm
        giam_don = float(order.get("total_discount") or 0)           # tổng giảm giá của đơn
        if GIA_TRU_GIAM_GIA:
            tong_giam = max(0.0, giam_don - giam_items)              # phần giảm giá từng sản phẩm đã trừ vào đơn giá rồi
        else:
            tong_giam = max(giam_don, giam_items)                    # đơn giá là giá gốc -> ghi toàn bộ giảm giá
        # thuế suất của dòng giảm giá = thuế suất bình quân của hàng trong đơn (theo giá trị)
        co_so = sum(x["don_gia"] * x["qty"] for x in items)
        thue_chung = (sum(x["don_gia"] * x["qty"] * x["thue"] for x in items) / co_so) if co_so > 0 else THUE_SUAT
        if GIA_DA_GOM_VAT: tong_giam /= (1 + thue_chung)
        if dong_don and round(tong_giam) > 0:
            gia_giam = -round(tong_giam)
            tien_thue_giam = round(gia_giam * thue_chung)
            tong_don += gia_giam + tien_thue_giam
            co_giam = [x for x in items if x["giam"] > 0]
            dong_don.append({"_id": base["id"], "id": base["id"], "ten_hang": TEN_HANG_GIAM_GIA,
                             "so_luong": 1, "don_gia": gia_giam, "thanh_tien": gia_giam,
                             "thue_suat": round(thue_chung, 4), "tien_thue": tien_thue_giam,
                             "giam_gia": True,
                             "_meta": {"goc": {x["gia_goc"] for x in co_giam}, "net": {x["gia_net"] for x in co_giam}}})

        if dong_don:
            # ID ghi ở mọi dòng; thông tin chung còn lại (ngày giao, khách, MST, nguồn, kho...) + Tổng hóa đơn: chỉ ở dòng đầu đơn
            dong_don[0].update(base)
            dong_don[0]["tong"] = tong_don
            cac_don.append((ngay_giao, dong_don))

    if so_ca_nhan:
        print(f"-> {so_ca_nhan} đơn chỉ có thẻ VAT cá nhân: bỏ thông tin công ty (MST, Tên công ty, Địa chỉ công ty)")

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


def _text(v):
    """Lấy chữ từ giá trị ô Lark (có thể là chuỗi, list, dict)."""
    if isinstance(v, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text", ""))
    return "" if v is None else str(v)


def _so(v):
    try: return round(float(_text(v).replace(",", "") or 0))
    except ValueError: return 0


def id_da_co(h, ids):
    """Chỉ tra các ID vừa quét được (không đọc cả bảng).
    Trả về dict {ID: {(tên hàng, đơn giá), ...}} = các dòng đơn đó ĐÃ có trên Lark."""
    url = f"{LARK}/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records/search"
    co = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        body = {"field_names": [COT["id"], COT["ten_hang"], COT["don_gia"]],
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
                f = it.get("fields") or {}
                oid = _text(f.get(COT["id"])).strip().removesuffix(".0")
                if not oid: continue
                co.setdefault(oid, set()).add((norm(_text(f.get(COT["ten_hang"]))), _so(f.get(COT["don_gia"]))))
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


def can_bo_sung(r, da_co_don):
    """Đơn đã có trên Lark: dòng r có cần thêm không?"""
    gia_da_co = {p for _, p in da_co_don}
    if r.get("giam_gia"):
        # Đã có dòng 'Sản phẩm tặng' rồi -> thôi
        if any(t == TEN_GIAM_GIA_NORM for t, _ in da_co_don): return False
        # Đơn cũ ghi theo kiểu đơn giá ĐÃ trừ giảm giá (chỉ thấy giá net, không thấy giá gốc)
        # -> thêm dòng âm nữa sẽ trừ 2 lần, nên bỏ qua
        meta = r["_meta"]
        if any(n in gia_da_co for n in meta["net"]) and not any(g in gia_da_co for g in meta["goc"]): return False
        return True
    ten = norm(r["ten_hang"])
    return not any((ten, p) in da_co_don for p in r["_gia_khop"])


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
    KEYS_DONG = ("_id", "id", "ma_hang", "ten_hang", "so_luong", "don_gia", "thanh_tien", "thue_suat", "tien_thue")
    moi, don_bu = [], set()
    for r in rows:
        if r["_id"] not in da_co:
            moi.append(r)                                        # đơn mới hoàn toàn
        elif can_bo_sung(r, da_co[r["_id"]]):
            moi.append({k: r[k] for k in KEYS_DONG if k in r})   # đơn cũ nhưng thiếu dòng này -> bổ sung
            don_bu.add(r["_id"])
    don_moi = ids - set(da_co)
    print(f"-> Đơn mới: {len(don_moi)} | Đơn cũ thiếu dòng cần bổ sung: {len(don_bu)} | Tổng dòng sẽ thêm: {len(moi)}")
    if not moi:
        print(f"Không có dòng nào cần thêm (mất {time.time() - t0:.0f} giây)."); return
    if DRY_RUN:
        for r in moi[:30]:
            print(f"   + {r['_id']} | {r['ten_hang']} | {r.get('ma_hang', '')!r} | SL {r['so_luong']} | giá {r['don_gia']}")

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
