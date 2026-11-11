import os
import time
import requests
import json
import ast
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

# Đọc từ biến môi trường (GitHub Secrets) — KHÔNG hardcode trong code
# Cần 5 secret: PANCAKE_API_KEY, PANCAKE_SHOP_ID, LARK_APP_ID, LARK_APP_SECRET, LARK_BASE_APP_TOKEN
PANCAKE_API_KEY = os.getenv("PANCAKE_API_KEY", "").strip()
PANCAKE_SHOP_ID = os.getenv("PANCAKE_SHOP_ID", "").strip()
LARK_APP_ID = os.getenv("LARK_APP_ID", "").strip()
LARK_APP_SECRET = os.getenv("LARK_APP_SECRET", "").strip()
LARK_APP_TOKEN = os.getenv("LARK_BASE_APP_TOKEN", "").strip()
LARK_TABLE_ID = os.getenv("LARK_TABLE_ID_ORDERS", "").strip() or "tblBzXKFjP0eIy8s"

COL_MUC_ME = "Các mục mẹ"
MAX_RETRIES = 3
MAX_PANCAKE_PAGES = 300
UPDATED_WITHIN_DAYS = 5  # QUÉT CÁC ĐƠN CẬP NHẬT TRONG 5 NGÀY QUA (tính đến lúc chạy)

# ================== TỐC ĐỘ ==================
PANCAKE_PAGE_SIZE = 500     # số đơn / trang khi tải Pancake (trước là 50)
SO_LUONG_SONG_SONG = 3      # số yêu cầu chạy song song (tải Pancake, tìm trên Lark)
LARK_TOI_DA_MOI_GIAY = 5    # giới hạn số lần gọi Lark / giây (chung cho mọi luồng) để không bị TooManyRequest
SO_ID_MOI_LAN_TIM = 20      # số ID gộp trong 1 lần tìm trên Lark (mỗi ID = 2 điều kiện ID / ID mới)
SO_DONG_MOI_LO_GHI = 500    # số dòng / 1 lần ghi Lark (trước là 100)
# BƯỚC 5 dọn mẹ trùng:
#   mặc định  = chỉ dọn trong các đơn vừa quét (nhanh, đủ cho chạy hằng ngày)
#   DON_TOAN_BANG=1 (biến môi trường) = đọc TOÀN BẢNG như trước (chậm, nên chạy định kỳ, vd 1 lần/tuần)
DON_TOAN_BANG = os.getenv("DON_TOAN_BANG", "").strip() == "1"

# BƯỚC 5: Xóa dòng mẹ trùng: 2 dòng mẹ cùng ID chốt (VD dòng 206847 đã ghi ID mới 207884, và 1 dòng 207884 do code tạo
# trước khi ghi ID mới). Giữ dòng nối tay, xóa dòng do code tạo.
XOA_ME_TRUNG = True
# True = dòng mẹ trùng do code tạo ĐÃ CÓ CON vẫn được xóa (xóa cả các dòng con của nó),
#        miễn là các dòng đó không có dữ liệu nhập tay khác với dòng được giữ.
# False = dòng trùng có con chỉ báo để kiểm tra tay (như trước).
XOA_ME_TRUNG_CO_CON = True

# Các cột do code tự ghi. Dữ liệu ở các cột này không tính là "nhập tay" khi xét xóa dòng trùng.
COT_CODE_GHI = {"ID", "Tên người mua", "CCCD", "MST", "Tên đơn vị", "Địa chỉ", "SĐT",
                "Email nhận hóa đơn", "Ngày điền xuất VAT", COL_MUC_ME}
# Loại cột Lark tự tính (công thức, tra cứu, ngày tạo/sửa, người tạo/sửa, số tự tăng): bỏ qua khi xét
LOAI_COT_TU_DONG = {19, 20, 1001, 1002, 1003, 1004, 1005}


def normalize_tag(name):
    # Chuẩn hóa Unicode tiếng Việt, bỏ khoảng trắng thừa, không phân biệt hoa/thường
    return " ".join(unicodedata.normalize("NFC", str(name or "")).lower().split())


def chuan_mst(mst):
    """Bỏ mọi khoảng trắng trong MST (dấu cách thừa, dấu cách đặc biệt khi copy từ web)."""
    return "".join(str(mst or "").split())


# CHỈ ĐỒNG BỘ ĐƠN CÓ MỘT TRONG CÁC THẺ NÀY
TAG_VAT_CA_NHAN = normalize_tag("VAT cá nhân")
TAG_VAT_CONG_TY = normalize_tag("VAT công ty")
VAT_TAGS = {TAG_VAT_CA_NHAN, TAG_VAT_CONG_TY}
TAG_KHAC_PHUC = normalize_tag("khắc phục")

# CÁC CỘT THÔNG TIN CÔNG TY / XUẤT HÓA ĐƠN
# - Đơn có thẻ "VAT công ty": ghi thông tin công ty từ POS.
# - Đơn CHỈ có thẻ "VAT cá nhân" (không có "VAT công ty"): KHÔNG xuất thông tin công ty,
#   và nếu bản ghi trên Lark đã có thông tin công ty (MST / Tên đơn vị) thì xóa cụm này đi
#   (MST, Tên đơn vị xóa trống; Địa chỉ, SĐT, Email thay bằng thông tin cá nhân từ POS).
# - Đơn không có thông tin công ty ở POS nhưng Lark đã có (sale tự điền tay) và đơn là "VAT công ty":
#   giữ nguyên, không ghi đè bằng dữ liệu cá nhân.
COMPANY_FIELDS = ["MST", "Tên đơn vị", "Địa chỉ", "SĐT", "Email nhận hóa đơn"]

# Thông tin công ty trên POS gắn theo ID KHÁCH, không gắn theo đơn. Khi 1 khách đặt cho 2 công ty khác nhau,
# thông tin công ty 2 sẽ đè lên công ty 1, và đơn 1 khi quét lại sẽ đọc ra thông tin công ty 2.
# -> Trước khi cập nhật cụm công ty cho 1 dòng đã có thông tin công ty trên Lark, kiểm tra xem POS có còn là
#    CÙNG công ty đó không. Cùng công ty (MST trùng, hoặc tên gần như y hệt do sửa lỗi gõ MST / sửa nhẹ thông tin)
#    -> cập nhật bình thường. Khác công ty -> giữ nguyên thông tin đã điền trên Lark, không ghi đè.
NGUONG_TEN_GIONG = 0.9  # độ giống tên đơn vị (0-1) để coi là cùng công ty khi MST khác nhau


def cung_cong_ty(lark_fields, pos_fields):
    """True nếu cụm công ty trên POS được phép ghi vào dòng Lark này."""
    mst_l = chuan_mst(extract_text(lark_fields.get("MST")))
    mst_p = chuan_mst(pos_fields.get("MST"))
    ten_l = normalize_tag(extract_text(lark_fields.get("Tên đơn vị")))
    ten_p = normalize_tag(pos_fields.get("Tên đơn vị"))

    # Lark chưa có thông tin công ty -> bổ sung bình thường
    if not mst_l and not ten_l: return True
    # Cùng MST (so 10 số đầu, 13 số là chi nhánh) -> cùng công ty, cho cập nhật các trường còn lại
    if mst_l and mst_p and mst_l[:10] == mst_p[:10]: return True
    # MST khác nhưng tên gần như y hệt -> sale sửa lỗi gõ MST / sửa nhẹ thông tin
    if ten_l and ten_p and SequenceMatcher(None, ten_l, ten_p).ratio() >= NGUONG_TEN_GIONG: return True
    return False


_khoa_lark = threading.Lock()
_lan_goi_lark_cuoi = [0.0]


def _cho_luot_lark():
    """Giãn các lần gọi Lark: tối đa LARK_TOI_DA_MOI_GIAY lần / giây, dùng chung cho mọi luồng."""
    with _khoa_lark:
        cho = _lan_goi_lark_cuoi[0] + 1.0 / LARK_TOI_DA_MOI_GIAY - time.time()
        if cho > 0: time.sleep(cho)
        _lan_goi_lark_cuoi[0] = time.time()


def request_with_retry(method, url, **kwargs):
    timeout = kwargs.pop("timeout", 30)
    la_lark = "larksuite.com" in url
    attempt, so_lan_qua_tai = 0, 0
    while attempt < MAX_RETRIES:
        attempt += 1
        try:
            if la_lark: _cho_luot_lark()
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return resp
                # Lark báo quá tải: chờ 1-2 giây rồi gọi lại, không tính vào số lần thử (tối đa 10 lần)
                if isinstance(body, dict) and body.get("code") in (1254290, 1254291) and so_lan_qua_tai < 10:
                    so_lan_qua_tai += 1; attempt -= 1
                    time.sleep(1 + so_lan_qua_tai * 0.5)
                    continue
                if isinstance(body, dict) and "code" in body and body["code"] != 0:
                    print(f"    [LARK LỖI code={body.get('code')}] {body.get('msg')} - thử lại ({attempt}/{MAX_RETRIES})...")
                else:
                    return resp
            elif resp.status_code == 429 and so_lan_qua_tai < 10:
                so_lan_qua_tai += 1; attempt -= 1
                time.sleep(1 + so_lan_qua_tai * 0.5)
                continue
            else:
                print(f"    [HTTP {resp.status_code}] thử lại ({attempt}/{MAX_RETRIES})...")
        except requests.exceptions.RequestException as e:
            print(f"    [Lỗi mạng] {type(e).__name__} - thử lại ({attempt}/{MAX_RETRIES})...")
        time.sleep(3 * attempt)
    return None


def extract_text(val):
    if val is None or val == "": return ""
    if isinstance(val, bool): return "1" if val else ""          # ô tích: chưa tích = trống
    if isinstance(val, (int, float)): return str(int(val) if val % 1 == 0 else val)
    if isinstance(val, str):
        val = val.strip()
        if (val.startswith("{") and val.endswith("}")) or (val.startswith("[") and val.endswith("]")):
            try: val = json.loads(val.replace("'", '"'))
            except Exception:
                try: val = ast.literal_eval(val)
                except Exception: pass
    if isinstance(val, list):
        if len(val) == 0: return ""
        return extract_text(val[0])
    if isinstance(val, dict):
        for key in ["value", "text", "name", "link", "id"]:
            if key in val: return extract_text(val[key])
        return ""
    return str(val).strip()


def get_number(val):
    try:
        if isinstance(val, (int, float)): return int(val)
        text_val = extract_text(val)
        return int(float(text_val)) if text_val else 1
    except Exception:
        return 1


def get_linked_parent_record_id(raw_muc_me):
    """Lấy record_id của dòng mẹ từ cột 'Các mục mẹ'. Đọc được các dạng Lark trả về:
    {"link_record_ids": [...]}, [{"record_ids": [...]}], ["recxxx"], chuỗi JSON..."""
    v = raw_muc_me
    if not v: return None
    if isinstance(v, str):
        t = v.strip()
        if t in ["", "[]", "{}", "None"]: return None
        try: v = json.loads(t.replace("'", '"'))
        except Exception:
            try: v = ast.literal_eval(t)
            except Exception: return t if t.startswith("rec") else None
    if isinstance(v, dict):
        for k in ("link_record_ids", "record_ids"):
            if v.get(k): return v[k][0]
        return v.get("record_id") or v.get("id")
    if isinstance(v, list):
        for item in v:
            r = get_linked_parent_record_id(item)
            if r: return r
    return None


def is_root_record(fields):
    """Dòng mẹ = cột 'Các mục mẹ' không trỏ tới dòng nào."""
    return get_linked_parent_record_id(fields.get(COL_MUC_ME)) is None


def get_lark_token():
    url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
    resp = request_with_retry("POST", url, json={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    return resp.json().get("tenant_access_token") if resp is not None else None


def _tim_mot_cum(headers, records_url, chunk_ids):
    """Tìm 1 cụm ID trên Lark, đọc hết các trang.
    page_size / page_token phải nằm trên URL (bản cũ để trong body nên Lark bỏ qua, mỗi trang chỉ trả 20 dòng)."""
    conditions = []
    for val in chunk_ids:
        conditions.append({"field_name": "ID", "operator": "is", "value": [val]})
        conditions.append({"field_name": "ID mới", "operator": "is", "value": [val]})
    payload = {"filter": {"conjunction": "or", "conditions": conditions}}
    out, page_token = [], ""
    while True:
        params = {"page_size": 500}
        if page_token: params["page_token"] = page_token
        resp = request_with_retry("POST", f"{records_url}/search", headers=headers, params=params, json=payload)
        if resp is None: break
        data = resp.json().get("data", {}) or {}
        out += data.get("items") or []
        if not data.get("has_more"): break
        page_token = data.get("page_token", "")
    return out


def _search_ids(headers, records_url, id_values, all_items, seen_record_ids):
    """Tìm theo cụm SO_ID_MOI_LAN_TIM ID, các cụm chạy song song."""
    ids = [str(v) for v in id_values]
    cum = [ids[i:i + SO_ID_MOI_LAN_TIM] for i in range(0, len(ids), SO_ID_MOI_LAN_TIM)]
    with ThreadPoolExecutor(max_workers=SO_LUONG_SONG_SONG) as ex:
        for items in ex.map(lambda c: _tim_mot_cum(headers, records_url, c), cum):
            for item in items:
                rid = item.get("record_id")
                if rid not in seen_record_ids:
                    seen_record_ids.add(rid)
                    all_items.append(item)


def search_lark_records_by_ids(headers, records_url, id_values):
    """Tìm các dòng trên Lark theo ID / ID mới.
    Lượt 2: dòng mẹ tìm được qua 'ID mới' nhưng cột 'ID' là số khác (VD mẹ ID 204335, ID mới 206594)
    -> dòng con của nó mang ID 204335 -> tìm thêm theo ID đó để không bỏ sót dòng con."""
    if not id_values: return []
    all_items, seen_record_ids = [], set()
    _search_ids(headers, records_url, list(id_values), all_items, seen_record_ids)

    da_tim = {str(v) for v in id_values}
    id_bo_sung = sorted({extract_text((it.get("fields") or {}).get("ID")) for it in all_items} - da_tim - {""})
    if id_bo_sung:
        _search_ids(headers, records_url, id_bo_sung, all_items, seen_record_ids)
    return all_items


def build_records_map_from_items(all_items):
    lark_roots = []
    sys_id_to_root = {}
    for item in all_items:
        fields = item.get("fields", {})
        rec_id = item.get("record_id")
        if is_root_record(fields):
            sys_id = extract_text(fields.get("ID"))
            id_moi = extract_text(fields.get("ID mới"))
            id_chot = extract_text(fields.get("ID chốt"))
            main_id = id_chot or id_moi or sys_id
            rec_data = {"record_id": rec_id, "fields": fields, "sub_count": 0, "child_records": [], "main_id": main_id}
            lark_roots.append(rec_data)

    # Ưu tiên: số ở 'ID mới' (dòng cũ đã nối tay sang đơn mới) thắng dòng mẹ trùng có cùng số ở 'ID'
    for r in lark_roots:
        id_moi = extract_text(r["fields"].get("ID mới"))
        if id_moi: sys_id_to_root[id_moi] = r
    for r in lark_roots:
        sys_id = extract_text(r["fields"].get("ID"))
        if sys_id: sys_id_to_root.setdefault(sys_id, r)

    record_id_to_data = {p["record_id"]: p for p in lark_roots}
    for item in all_items:
        fields = item.get("fields", {})
        if is_root_record(fields): continue
        child_sys_id = extract_text(fields.get("ID"))
        child_id_chot = extract_text(fields.get("ID chốt"))
        child_id_moi = extract_text(fields.get("ID mới"))
        matched_parent_data = None
        linked_rec_id = get_linked_parent_record_id(fields.get(COL_MUC_ME))
        if linked_rec_id and linked_rec_id in record_id_to_data:
            matched_parent_data = record_id_to_data[linked_rec_id]
        else:
            if child_id_chot and child_id_chot in sys_id_to_root: matched_parent_data = sys_id_to_root[child_id_chot]
            elif child_id_moi and child_id_moi in sys_id_to_root: matched_parent_data = sys_id_to_root[child_id_moi]
            elif child_sys_id and child_sys_id in sys_id_to_root: matched_parent_data = sys_id_to_root[child_sys_id]
        if matched_parent_data:
            child_rec_id = item.get("record_id")
            if child_rec_id not in matched_parent_data["child_records"]:
                matched_parent_data["sub_count"] += 1
                matched_parent_data["child_records"].append(child_rec_id)
    return lark_roots, sys_id_to_root


def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def batch_execute_lark(headers, records_url, items, action="create", ket_qua=None):
    """Ghi theo lô SO_DONG_MOI_LO_GHI dòng.
    ket_qua (list): nếu truyền vào, thêm {record_id, fields} của các dòng ghi thành công (để bước sau khỏi tìm lại)."""
    count = 0
    endpoint = f"{records_url}/batch_create" if action == "create" else f"{records_url}/batch_update"
    for chunk in chunk_list(items, SO_DONG_MOI_LO_GHI):
        resp = request_with_retry("POST", endpoint, headers=headers, json={"records": chunk}, timeout=120)
        if resp is not None:
            recs = resp.json().get("data", {}).get("records", []) or []
            count += len(recs)
            if ket_qua is not None:
                for goc, rec in zip(chunk, recs):
                    if rec.get("record_id"):
                        ket_qua.append({"record_id": rec["record_id"], "fields": dict(goc.get("fields") or {})})
    return count


def lay_toan_bang(headers, records_url):
    items, page_token = [], ""
    while True:
        params = {"page_size": 500}
        if page_token: params["page_token"] = page_token
        resp = request_with_retry("GET", records_url, headers=headers, params=params, timeout=120)
        if resp is None: break
        d = resp.json().get("data", {}) or {}
        items += d.get("items") or []
        if not d.get("has_more"): break
        page_token = d.get("page_token")
    return items


def lay_loai_cot(headers, records_url):
    """{tên cột: loại cột} của bảng, để phân biệt cột nhập tay với cột công thức / tra cứu."""
    url = records_url.rsplit("/records", 1)[0] + "/fields"
    loai, page_token = {}, ""
    while True:
        params = {"page_size": 100}
        if page_token: params["page_token"] = page_token
        resp = request_with_retry("GET", url, headers=headers, params=params)
        if resp is None: break
        d = resp.json().get("data", {}) or {}
        for f in d.get("items") or []:
            loai[f.get("field_name")] = f.get("type")
        if not d.get("has_more"): break
        page_token = d.get("page_token")
    return loai


def so_o_co_du_lieu(fields):
    return sum(1 for v in (fields or {}).values() if extract_text(v))


def gia_tri_nhap_tay(fields, loai_cot):
    """{cột: giá trị} các ô có dữ liệu ở cột nhập tay (không phải cột code ghi, không phải công thức / tra cứu)."""
    out = {}
    for col, v in (fields or {}).items():
        if col in COT_CODE_GHI or loai_cot.get(col) in LOAI_COT_TU_DONG:
            continue
        t = extract_text(v)
        if t: out[col] = t
    return out


def xoa_records(headers, records_url, ids):
    n = 0
    for chunk in chunk_list(ids, 500):
        resp = request_with_retry("POST", f"{records_url}/batch_delete", headers=headers, json={"records": chunk})
        if resp is not None: n += len(chunk)
    return n


def chon_me_chinh(ds, main_id):
    """Trong các dòng mẹ cùng ID chốt: ưu tiên dòng có ID mới nối tay, rồi nhiều con, rồi nhiều dữ liệu."""
    def diem(r):
        noi_tay = 1 if extract_text(r["fields"].get("ID")) != main_id else 0
        return (noi_tay, r["sub_count"], so_o_co_du_lieu(r["fields"]))
    return sorted(ds, key=diem, reverse=True)


def don_dep_toan_bang(headers, records_url, items=None):
    """BƯỚC 5: xử lý dòng mẹ trùng ID chốt.
    items = None -> đọc toàn bảng (như trước); có items -> chỉ xét các dòng của các đơn vừa quét.
    - Giữ dòng 'chính' (có ID mới nối tay / nhiều con / nhiều dữ liệu).
    - Chép MST, Tên đơn vị... sang dòng chính nếu dòng chính còn trống.
    - Xóa dòng trùng KHÔNG có con (như trước).
    - Dòng trùng CÓ con (VD code tạo dòng 207884 + con trước khi sale ghi ID mới vào dòng 206847):
      nếu dòng trùng do code tạo (cột ID = ID chốt) và cả dòng mẹ lẫn các dòng con của nó không có dữ liệu
      nhập tay nào khác với dòng được giữ -> xóa cả mẹ trùng và các con của nó.
      Ngược lại -> chỉ báo để kiểm tra tay, nêu rõ cột nào đang có dữ liệu riêng."""
    if items is None:
        print("\n--- BƯỚC 5: DỌN TOÀN BẢNG (mẹ trùng ID chốt) ---")
        items = lay_toan_bang(headers, records_url)
    else:
        print("\n--- BƯỚC 5: DỌN MẸ TRÙNG TRONG CÁC ĐƠN VỪA QUÉT ---")
    loai_cot = lay_loai_cot(headers, records_url)
    fields_theo_rec = {it.get("record_id"): it.get("fields") or {} for it in items}
    roots, _ = build_records_map_from_items(items)
    print(f"-> Xét {len(items)} dòng, {len(roots)} dòng mẹ")

    theo_id_chot = {}
    for r in roots:
        if r.get("main_id"): theo_id_chot.setdefault(r["main_id"], []).append(r)

    me_xoa, con_xoa, cap_nhat, can_kiem_tra = [], [], [], []
    for main_id, ds in theo_id_chot.items():
        if len(ds) < 2: continue
        ds = chon_me_chinh(ds, main_id)
        giu, trung = ds[0], ds[1:]

        # Dữ liệu nhập tay đang có ở dòng được giữ (mẹ + con), để so với dòng trùng
        da_co_o_giu = {}
        for rec in [giu["record_id"]] + giu["child_records"]:
            for col, t in gia_tri_nhap_tay(fields_theo_rec.get(rec), loai_cot).items():
                da_co_o_giu.setdefault(col, set()).add(t)

        bo_sung = {}
        so_xoa = 0
        for t in trung:
            for col in COMPANY_FIELDS:
                v_giu, v_t = extract_text(giu["fields"].get(col)), extract_text(t["fields"].get(col))
                if not v_giu and v_t and col not in bo_sung:
                    bo_sung[col] = chuan_mst(v_t) if col == "MST" else v_t

            id_dong = extract_text(t["fields"].get("ID"))
            if t["sub_count"] == 0:
                me_xoa.append(t["record_id"]); so_xoa += 1
                continue

            # Dòng trùng có con
            do_code_tao = id_dong == main_id
            cot_rieng = set()
            for rec in [t["record_id"]] + t["child_records"]:
                for col, val in gia_tri_nhap_tay(fields_theo_rec.get(rec), loai_cot).items():
                    if val not in da_co_o_giu.get(col, set()):
                        cot_rieng.add(col)

            if XOA_ME_TRUNG_CO_CON and do_code_tao and not cot_rieng:
                me_xoa.append(t["record_id"]); con_xoa += t["child_records"]; so_xoa += 1
            else:
                ly_do = ("dòng nối tay, không phải dòng code tạo" if not do_code_tao
                         else f"có dữ liệu nhập tay riêng ở cột: {', '.join(sorted(cot_rieng))}" if cot_rieng
                         else "đang tắt XOA_ME_TRUNG_CO_CON")
                can_kiem_tra.append(f"{main_id} (dòng ID {id_dong}, {t['sub_count']} con: {ly_do})")

        if bo_sung:
            cap_nhat.append({"record_id": giu["record_id"], "fields": bo_sung})
            for c in giu["child_records"]:
                cap_nhat.append({"record_id": c, "fields": bo_sung})
        print(f"   ID chốt {main_id}: giữ dòng ID {extract_text(giu['fields'].get('ID'))}, {so_xoa} dòng mẹ trùng sẽ xóa")
    if can_kiem_tra:
        print("   !! Dòng mẹ trùng cần kiểm tra tay: " + "; ".join(can_kiem_tra))

    if cap_nhat: batch_execute_lark(headers, records_url, cap_nhat, action="update")
    if not XOA_ME_TRUNG:
        if me_xoa: print(f"   (Đang tắt xóa: chỉ báo {len(me_xoa)} dòng mẹ trùng, không xóa)")
        return 0
    # Xóa con trước, rồi mới xóa mẹ
    xoa_con = xoa_records(headers, records_url, con_xoa) if con_xoa else 0
    xoa_me = xoa_records(headers, records_url, me_xoa) if me_xoa else 0
    print(f"-> Xóa {xoa_me} dòng mẹ trùng, {xoa_con} dòng con của mẹ trùng")
    return xoa_me


def tai_don_pancake(start_ts, end_ts):
    """Tải trang 1 để biết tổng số trang, rồi tải các trang còn lại song song. Trả về list đơn theo thứ tự trang."""
    url = f"https://pos.pages.fm/api/v1/shops/{PANCAKE_SHOP_ID}/orders"

    def tai_trang(page):
        params = {"api_key": PANCAKE_API_KEY, "page_number": page, "page_size": PANCAKE_PAGE_SIZE,
                  "status": "-1", "updateStatus": "updated_at",
                  "startDateTime": start_ts, "endDateTime": end_ts}
        resp = request_with_retry("GET", url, params=params, timeout=120)
        if resp is None:
            print(f"    !! Bỏ qua trang {page} sau {MAX_RETRIES} lần lỗi")
            return {}
        return resp.json() or {}

    trang_1 = tai_trang(1)
    tong_trang = min(int(trang_1.get("total_pages") or 1), MAX_PANCAKE_PAGES)
    orders = list(trang_1.get("data") or [])
    if tong_trang > 1:
        with ThreadPoolExecutor(max_workers=SO_LUONG_SONG_SONG) as ex:
            for body in ex.map(tai_trang, range(2, tong_trang + 1)):
                orders.extend(body.get("data") or [])
    print(f"-> Tải {len(orders)} đơn ({tong_trang} trang)")
    return orders


def main():
    t0 = time.time()
    thieu = [k for k, v in {"PANCAKE_API_KEY": PANCAKE_API_KEY, "PANCAKE_SHOP_ID": PANCAKE_SHOP_ID,
                            "LARK_APP_ID": LARK_APP_ID, "LARK_APP_SECRET": LARK_APP_SECRET,
                            "LARK_BASE_APP_TOKEN": LARK_APP_TOKEN}.items() if not v]
    if thieu:
        raise SystemExit(f"🛑 Thiếu biến môi trường: {', '.join(thieu)} (kiểm tra secret và phần env trong file yml).")
    lark_token = get_lark_token()
    if not lark_token:
        raise SystemExit("🛑 Lấy Lark token thất bại — kiểm tra LARK_APP_ID / LARK_APP_SECRET.")
    headers = {"Authorization": f"Bearer {lark_token}", "Content-Type": "application/json"}
    records_url = f"https://open.larksuite.com/open-apis/bitable/v1/apps/{LARK_APP_TOKEN}/tables/{LARK_TABLE_ID}/records"

    # XỬ LÝ TIMEZONE CHO GITHUB ACTIONS: Dùng timestamp tuyệt đối và giờ VN (UTC+7)
    end_ts = int(time.time())
    start_ts = end_ts - (UPDATED_WITHIN_DAYS * 24 * 3600)

    vn_time = datetime.now(timezone.utc) + timedelta(hours=7)
    today_str = vn_time.strftime("%Y/%m/%d")

    print(f"\n--- BƯỚC 1: TẢI ĐƠN PANCAKE (Cập nhật trong {UPDATED_WITHIN_DAYS} ngày qua) ---")

    all_valid_orders = []
    for order in tai_don_pancake(start_ts, end_ts):
        # LỌC THẺ: chỉ lấy đơn có "VAT cá nhân" hoặc "VAT công ty"
        tags = order.get("tags") or []
        tag_names = {normalize_tag(t.get("name")) for t in tags if isinstance(t, dict)}
        has_vat = bool(tag_names & VAT_TAGS)
        has_khac_phuc = TAG_KHAC_PHUC in tag_names

        if not has_vat: continue

        # CHỈ "VAT cá nhân" (không có "VAT công ty") -> không xuất thông tin công ty
        chi_ca_nhan = (TAG_VAT_CA_NHAN in tag_names) and (TAG_VAT_CONG_TY not in tag_names)

        system_id = str(order.get("system_id") or "").strip()
        if not system_id: continue

        items = order.get("items") or []
        total_qty = sum(int(it.get("quantity") or 0) for it in items)
        if total_qty <= 0: continue

        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        company = customer.get("company_info") or {}

        phones = customer.get("phone_numbers") or []
        cust_phone = phones[0] if phones else ""
        ship_phone = shipping.get("phone_number") or ""
        comp_phone = company.get("company_phone") if company else ""

        customer_phone = str(ship_phone).strip() if str(ship_phone).strip() else str(cust_phone).strip()
        company_phone = "" if chi_ca_nhan else str(comp_phone or "").strip()

        if not customer_phone and not company_phone: continue

        cust_emails = customer.get("emails") or []
        order_email = order.get("bill_email") or (cust_emails[0] if cust_emails else "")

        desired_fields = {
            "Tên người mua": shipping.get("full_name") or customer.get("name") or "",
            "CCCD": customer.get("identity_code") or "",
        }

        # Đơn chỉ "VAT cá nhân": bỏ qua company_info của POS dù khách có lưu thông tin công ty
        pos_has_company = bool(company and company.get("company_name")) and not chi_ca_nhan

        if pos_has_company:
            desired_fields["MST"] = chuan_mst(company.get("company_tax_id"))
            desired_fields["Tên đơn vị"] = company.get("company_name") or ""
            desired_fields["Địa chỉ"] = company.get("company_address") or ""
            desired_fields["SĐT"] = company_phone if company_phone else customer_phone
            desired_fields["Email nhận hóa đơn"] = company.get("company_email") or order_email or ""
        else:
            desired_fields["Địa chỉ"] = shipping.get("full_address") or ""
            desired_fields["SĐT"] = customer_phone if customer_phone else company_phone
            desired_fields["MST"] = ""
            desired_fields["Tên đơn vị"] = ""
            desired_fields["Email nhận hóa đơn"] = order_email or ""

        all_valid_orders.append({
            "system_id": system_id,
            "fields": desired_fields,
            "has_khac_phuc": has_khac_phuc,
            "has_company": pos_has_company,
            "chi_ca_nhan": chi_ca_nhan
        })

    all_valid_orders.reverse()
    pancake_data_map = {item["system_id"]: item for item in all_valid_orders}
    system_ids_to_fetch = list(pancake_data_map.keys())

    print(f"-> Đã gom được {len(all_valid_orders)} đơn đạt chuẩn từ Pancake. ({time.time() - t0:.0f} giây)")

    print(f"\n--- BƯỚC 2: TRA CỨU ID LIÊN QUAN TRÊN LARK ---")
    matched_items = search_lark_records_by_ids(headers, records_url, system_ids_to_fetch)
    lark_roots, sys_id_to_root = build_records_map_from_items(matched_items)
    print(f"-> Tìm thấy {len(matched_items)} bản ghi có liên quan trên Lark. ({time.time() - t0:.0f} giây)")

    print("\n--- BƯỚC 3: PHÂN LOẠI & CẬP NHẬT/TẠO MỚI ---")
    moms_to_create = []
    records_to_update = []
    so_xoa_cong_ty = 0

    for lark_record in lark_roots:
        main_id = lark_record.get("main_id")
        if main_id in pancake_data_map:
            pos_order = pancake_data_map[main_id]
            fields_to_apply = pos_order["fields"]
            lark_fields = lark_record["fields"]
            chi_ca_nhan = pos_order["chi_ca_nhan"]

            # Trên Lark đã có thông tin công ty (thường là tự điền tay)?
            lark_has_company = bool(extract_text(lark_fields.get("MST")) or extract_text(lark_fields.get("Tên đơn vị")))

            # POS đang trả về công ty KHÁC với công ty đã điền trên Lark (khách đặt tiếp cho công ty thứ 2)
            # -> bỏ qua cả cụm công ty, giữ nguyên dữ liệu đơn này
            bo_qua_cong_ty = pos_order["has_company"] and not cung_cong_ty(lark_fields, fields_to_apply)
            if bo_qua_cong_ty:
                print(f"   [GIỮ NGUYÊN] Đơn {main_id}: POS đang là công ty khác "
                      f"(POS: {fields_to_apply.get('MST')} / Lark: {extract_text(lark_fields.get('MST'))}), không ghi đè.")

            update_payload = {}
            for key, val in fields_to_apply.items():
                if bo_qua_cong_ty and key in COMPANY_FIELDS: continue
                new_val = str(val or "").strip()

                # 0. Đơn chỉ "VAT cá nhân" mà Lark đang có thông tin công ty -> xóa cụm công ty:
                #    MST / Tên đơn vị về trống; Địa chỉ / SĐT / Email thay bằng thông tin cá nhân (kể cả trống)
                if chi_ca_nhan and lark_has_company and key in COMPANY_FIELDS:
                    current_val = extract_text(lark_fields.get(key))
                    if str(current_val) != new_val:
                        update_payload[key] = new_val
                    continue

                # 1. Không bao giờ ghi ô trống đè lên dữ liệu trên Lark
                if not new_val: continue

                # 2. POS không có thông tin công ty nhưng Lark đã có -> giữ nguyên cụm thông tin công ty trên Lark
                #    (chỉ áp dụng cho đơn "VAT công ty"; đơn chỉ "VAT cá nhân" đã được xử lý ở mục 0)
                if not pos_order["has_company"] and lark_has_company and key in COMPANY_FIELDS: continue

                current_val = extract_text(lark_fields.get(key))
                if str(current_val) != new_val:
                    update_payload[key] = new_val

            if chi_ca_nhan and lark_has_company and update_payload:
                so_xoa_cong_ty += 1

            # 3. MST sale tự điền trên Lark có khoảng trắng -> làm sạch
            mst_lark = extract_text(lark_fields.get("MST"))
            if "MST" not in update_payload and mst_lark and mst_lark != chuan_mst(mst_lark):
                update_payload["MST"] = chuan_mst(mst_lark)

            if update_payload:
                records_to_update.append({"record_id": lark_record["record_id"], "fields": update_payload})
                for child_id in lark_record.get("child_records", []):
                    records_to_update.append({"record_id": child_id, "fields": update_payload})
                lark_record["fields"].update(update_payload)

    for order_data in all_valid_orders:
        system_id = order_data["system_id"]
        desired_fields = order_data["fields"]
        has_khac_phuc = order_data["has_khac_phuc"]

        if system_id not in sys_id_to_root:
            if has_khac_phuc: continue
            else:
                desired_fields = {k: v for k, v in desired_fields.items() if str(v or "").strip()}
                desired_fields["ID"] = system_id
                desired_fields["Ngày điền xuất VAT"] = today_str
                moms_to_create.append({"fields": desired_fields})
                sys_id_to_root[system_id] = True

    print(f"-> Chuẩn bị cập nhật {len(records_to_update)} dòng và tạo mới {len(moms_to_create)} đơn mẹ...")
    if so_xoa_cong_ty:
        print(f"-> Trong đó {so_xoa_cong_ty} đơn mẹ chỉ có thẻ VAT cá nhân sẽ bị xóa thông tin công ty.")
    updated_count = batch_execute_lark(headers, records_url, records_to_update, action="update") if records_to_update else 0
    added_moms_count = batch_execute_lark(headers, records_url, moms_to_create, action="create") if moms_to_create else 0
    print(f"   ({time.time() - t0:.0f} giây)")

    print("\n--- BƯỚC 4: KIỂM TRA & TẠO BẢN GHI CON THIẾU ---")
    # Không tìm lại toàn bộ: dữ liệu bước 2 đã có (đã cập nhật trong bộ nhớ ở bước 3).
    # Chỉ tìm lại các đơn mẹ vừa tạo, để Lark tính xong cột "Số hàng hóa" cho chúng.
    fresh_items = list(matched_items)
    if moms_to_create:
        id_me_moi = [m["fields"]["ID"] for m in moms_to_create]
        da_co = {it.get("record_id") for it in fresh_items}
        for it in search_lark_records_by_ids(headers, records_url, id_me_moi):
            if it.get("record_id") not in da_co:
                da_co.add(it.get("record_id"))
                fresh_items.append(it)
    fresh_lark_roots, _ = build_records_map_from_items(fresh_items)

    seen_rec_ids = set()
    subs_to_create = []

    # Dòng mẹ trùng ID chốt: chỉ tạo con cho dòng chính, dòng trùng để bước 5 xử lý
    nhom_me = {}
    for r in fresh_lark_roots:
        if r.get("main_id"): nhom_me.setdefault(r["main_id"], []).append(r)
    me_trung = {t["record_id"] for k, ds in nhom_me.items() if len(ds) > 1 for t in chon_me_chinh(ds, k)[1:]}

    for parent_info in fresh_lark_roots:
        rec_id = parent_info["record_id"]
        if rec_id in seen_rec_ids or rec_id in me_trung: continue
        seen_rec_ids.add(rec_id)

        old_fields = parent_info["fields"]
        so_hang_hoa = get_number(old_fields.get("Số hàng hóa"))
        needed_subs = max(0, so_hang_hoa - 1) - parent_info["sub_count"]

        if needed_subs > 0:
            safe_sub_fields = {COL_MUC_ME: [rec_id]}
            parent_id_value = extract_text(old_fields.get("ID")) or parent_info.get("main_id")
            if parent_id_value: safe_sub_fields["ID"] = parent_id_value
            for col in ["Tên người mua", "CCCD", "MST", "Tên đơn vị", "Địa chỉ", "SĐT", "Email nhận hóa đơn", "Ngày điền xuất VAT"]:
                val = extract_text(old_fields.get(col))
                if col == "MST": val = chuan_mst(val)
                if val: safe_sub_fields[col] = val
            for _ in range(needed_subs):
                subs_to_create.append({"fields": safe_sub_fields})

    con_moi = []
    added_subs_count = batch_execute_lark(headers, records_url, subs_to_create, action="create",
                                          ket_qua=con_moi) if subs_to_create else 0
    fresh_items += con_moi   # dòng con vừa tạo -> đưa vào để bước 5 tính đúng số con

    print(f"-> Thêm con: {added_subs_count} ({time.time() - t0:.0f} giây)")

    if DON_TOAN_BANG:
        xoa_me = don_dep_toan_bang(headers, records_url)
    else:
        xoa_me = don_dep_toan_bang(headers, records_url, items=fresh_items)
    print(f"\n✅ HOÀN TẤT TOÀN BỘ sau {time.time() - t0:.0f} giây! Thêm mẹ: {added_moms_count} | Cập nhật dòng: {updated_count} "
          f"| Thêm con: {added_subs_count} | Xóa mẹ trùng: {xoa_me}.")


if __name__ == "__main__":
    main()