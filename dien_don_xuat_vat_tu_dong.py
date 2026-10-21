import os
import time
import requests
import json
import ast
import unicodedata
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

# BƯỚC 5: DỌN TOÀN BẢNG (chạy mỗi lần, không giới hạn 5 ngày)
# Xóa dòng con thừa (mẹ có nhiều con hơn Số hàng hóa - 1)
XOA_CON_THUA = True
# Xóa dòng mẹ trùng: 2 dòng mẹ cùng ID chốt (VD dòng 205887 có ID mới 206292 và 1 dòng 206292 do code tạo)
XOA_ME_TRUNG = True


def normalize_tag(name):
    # Chuẩn hóa Unicode tiếng Việt, bỏ khoảng trắng thừa, không phân biệt hoa/thường
    return " ".join(unicodedata.normalize("NFC", str(name or "")).lower().split())


def chuan_mst(mst):
    """Bỏ mọi khoảng trắng trong MST (dấu cách thừa, dấu cách đặc biệt khi copy từ web)."""
    return "".join(str(mst or "").split())


# CHỈ ĐỒNG BỘ ĐƠN CÓ MỘT TRONG CÁC THẺ NÀY
VAT_TAGS = {normalize_tag(x) for x in ["VAT cá nhân", "VAT công ty"]}
TAG_KHAC_PHUC = normalize_tag("khắc phục")

# CÁC CỘT THÔNG TIN CÔNG TY / XUẤT HÓA ĐƠN
# Nếu trên Lark đã có MST hoặc Tên đơn vị (tự điền tay) mà đơn POS không có thông tin công ty
# thì KHÔNG ghi đè các cột này bằng dữ liệu cá nhân từ POS.
COMPANY_FIELDS = ["MST", "Tên đơn vị", "Địa chỉ", "SĐT", "Email nhận hóa đơn"]


def request_with_retry(method, url, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    return resp
                if isinstance(body, dict) and "code" in body and body["code"] != 0:
                    print(f"    [LARK LỖI code={body.get('code')}] {body.get('msg')} - thử lại ({attempt}/{MAX_RETRIES})...")
                else:
                    return resp
            else:
                print(f"    [HTTP {resp.status_code}] thử lại ({attempt}/{MAX_RETRIES})...")
        except requests.exceptions.RequestException as e:
            print(f"    [Lỗi mạng] {type(e).__name__} - thử lại ({attempt}/{MAX_RETRIES})...")
        time.sleep(3 * attempt)
    return None


def extract_text(val):
    if val is None or val == "": return ""
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


def get_number_or_none(val):
    """Như get_number nhưng ô trống / lỗi -> None (không đoán là 1)."""
    try:
        if isinstance(val, (int, float)): return int(val)
        t = extract_text(val)
        return int(float(t)) if t else None
    except Exception:
        return None


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
    return resp.json().get("tenant_access_token")


def _search_ids(headers, records_url, id_values, all_items, seen_record_ids):
    CHUNK = 15
    for i in range(0, len(id_values), CHUNK):
        chunk_ids = [str(v) for v in id_values[i:i + CHUNK]]
        conditions = []
        for val in chunk_ids:
            conditions.append({"field_name": "ID", "operator": "is", "value": [val]})
            conditions.append({"field_name": "ID mới", "operator": "is", "value": [val]})

        payload = {"filter": {"conjunction": "or", "conditions": conditions}, "page_size": 500}
        page_token = ""
        has_more = True
        while has_more:
            if page_token: payload["page_token"] = page_token
            resp = request_with_retry("POST", f"{records_url}/search", headers=headers, json=payload)
            if resp is None: break
            data = resp.json().get("data", {})
            for item in data.get("items", []):
                rid = item.get("record_id")
                if rid not in seen_record_ids:
                    seen_record_ids.add(rid)
                    all_items.append(item)
            has_more = data.get("has_more", False)
            page_token = data.get("page_token", "")


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


def batch_execute_lark(headers, records_url, items, action="create"):
    count = 0
    endpoint = f"{records_url}/batch_create" if action == "create" else f"{records_url}/batch_update"
    for chunk in chunk_list(items, 100):
        resp = request_with_retry("POST", endpoint, headers=headers, json={"records": chunk})
        if resp is not None:
            count += len(resp.json().get("data", {}).get("records", []))
    return count


def lay_toan_bang(headers, records_url):
    items, page_token = [], ""
    while True:
        params = {"page_size": 500}
        if page_token: params["page_token"] = page_token
        resp = request_with_retry("GET", records_url, headers=headers, params=params)
        if resp is None: break
        d = resp.json().get("data", {}) or {}
        items += d.get("items") or []
        if not d.get("has_more"): break
        page_token = d.get("page_token")
    return items


def so_o_co_du_lieu(fields):
    return sum(1 for v in (fields or {}).values() if extract_text(v))


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


def don_dep_toan_bang(headers, records_url):
    """BƯỚC 5: quét toàn bảng
    a) Dòng mẹ trùng ID chốt: giữ dòng 'chính' (có ID mới nối tay / nhiều con / nhiều dữ liệu),
       chép MST, Tên đơn vị... sang dòng chính nếu dòng chính còn trống, rồi xóa dòng trùng KHÔNG có con.
       Dòng trùng có con -> chỉ báo để kiểm tra tay.
    b) Dòng con thừa: mẹ có nhiều con hơn Số hàng hóa - 1 -> xóa các con ít dữ liệu nhất.
       Mẹ để trống Số hàng hóa -> bỏ qua, không xóa."""
    print("\n--- BƯỚC 5: DỌN TOÀN BẢNG (mẹ trùng ID chốt, con thừa) ---")
    items = lay_toan_bang(headers, records_url)
    roots, _ = build_records_map_from_items(items)
    fields_by_rec = {it.get("record_id"): (it.get("fields") or {}) for it in items}
    print(f"-> Bảng có {len(items)} dòng, {len(roots)} dòng mẹ")

    # a) Mẹ trùng ID chốt
    theo_id_chot = {}
    for r in roots:
        if r.get("main_id"): theo_id_chot.setdefault(r["main_id"], []).append(r)

    me_xoa, cap_nhat, can_kiem_tra = [], [], []
    bi_xoa = set()
    for main_id, ds in theo_id_chot.items():
        if len(ds) < 2: continue
        ds = chon_me_chinh(ds, main_id)
        giu, trung = ds[0], ds[1:]
        bo_sung = {}
        for t in trung:
            for col in COMPANY_FIELDS:
                v_giu, v_t = extract_text(giu["fields"].get(col)), extract_text(t["fields"].get(col))
                if not v_giu and v_t and col not in bo_sung:
                    bo_sung[col] = chuan_mst(v_t) if col == "MST" else v_t
            if t["sub_count"] == 0:
                me_xoa.append(t["record_id"]); bi_xoa.add(t["record_id"])
            else:
                can_kiem_tra.append(f"{main_id} (dòng ID {extract_text(t['fields'].get('ID'))} có {t['sub_count']} con)")
        if bo_sung:
            cap_nhat.append({"record_id": giu["record_id"], "fields": bo_sung})
            for c in giu["child_records"]:
                cap_nhat.append({"record_id": c, "fields": bo_sung})
        print(f"   ID chốt {main_id}: giữ dòng ID {extract_text(giu['fields'].get('ID'))}, "
              f"{len([t for t in trung if t['record_id'] in bi_xoa])} dòng trùng sẽ xóa")
    if can_kiem_tra:
        print("   !! Dòng mẹ trùng nhưng có con, cần kiểm tra tay: " + "; ".join(can_kiem_tra))

    # b) Con thừa
    con_xoa, dem = [], {}
    for r in roots:
        if r["record_id"] in bi_xoa: continue
        so_hh = get_number_or_none(r["fields"].get("Số hàng hóa"))
        if so_hh is None: continue
        can = max(0, so_hh - 1)
        if r["sub_count"] > can:
            cac_con = sorted(r["child_records"], key=lambda c: -so_o_co_du_lieu(fields_by_rec.get(c)))
            thua = cac_con[can:]
            con_xoa += thua
            dem[r.get("main_id")] = len(thua)
    if dem:
        print("   Con thừa: " + ", ".join(f"đơn {k}: {v} dòng" for k, v in dem.items()))

    se_xoa = set(con_xoa) if XOA_CON_THUA else set()
    cap_nhat = [u for u in cap_nhat if u["record_id"] not in se_xoa]
    if cap_nhat: batch_execute_lark(headers, records_url, cap_nhat, action="update")
    xoa_me = xoa_records(headers, records_url, me_xoa) if (XOA_ME_TRUNG and me_xoa) else 0
    xoa_con = xoa_records(headers, records_url, con_xoa) if (XOA_CON_THUA and con_xoa) else 0
    if (me_xoa and not XOA_ME_TRUNG) or (con_xoa and not XOA_CON_THUA):
        print("   (Đang tắt xóa: chỉ báo, không xóa)")
    print(f"-> Xóa {xoa_me} dòng mẹ trùng, {xoa_con} dòng con thừa")
    return xoa_me, xoa_con


def main():
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

    page_num = 1
    keep_fetching = True
    all_valid_orders = []

    while keep_fetching and page_num <= MAX_PANCAKE_PAGES:
        url = f"https://pos.pages.fm/api/v1/shops/{PANCAKE_SHOP_ID}/orders"

        api_params = {
            "api_key": PANCAKE_API_KEY, "page_number": page_num, "page_size": 50,
            "status": "-1",
            "updateStatus": "updated_at",
            "startDateTime": start_ts, "endDateTime": end_ts
        }
        resp = request_with_retry("GET", url, params=api_params)
        if resp is None: break

        orders = resp.json().get("data", [])
        if not orders: break

        for order in orders:
            # LỌC THẺ: chỉ lấy đơn có "VAT cá nhân" hoặc "VAT công ty"
            tags = order.get("tags") or []
            tag_names = {normalize_tag(t.get("name")) for t in tags if isinstance(t, dict)}
            has_vat = bool(tag_names & VAT_TAGS)
            has_khac_phuc = TAG_KHAC_PHUC in tag_names

            if not has_vat: continue

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
            company_phone = str(comp_phone or "").strip()

            if not customer_phone and not company_phone: continue

            cust_emails = customer.get("emails") or []
            order_email = order.get("bill_email") or (cust_emails[0] if cust_emails else "")

            desired_fields = {
                "Tên người mua": shipping.get("full_name") or customer.get("name") or "",
                "CCCD": customer.get("identity_code") or "",
            }

            pos_has_company = bool(company and company.get("company_name"))

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
                "has_company": pos_has_company
            })

        page_num += 1

    all_valid_orders.reverse()
    pancake_data_map = {item["system_id"]: item for item in all_valid_orders}
    system_ids_to_fetch = list(pancake_data_map.keys())

    print(f"-> Đã gom được {len(all_valid_orders)} đơn đạt chuẩn từ Pancake.")

    print(f"\n--- BƯỚC 2: TRA CỨU ID LIÊN QUAN TRÊN LARK ---")
    matched_items = search_lark_records_by_ids(headers, records_url, system_ids_to_fetch)
    lark_roots, sys_id_to_root = build_records_map_from_items(matched_items)
    print(f"-> Tìm thấy {len(matched_items)} bản ghi có liên quan trên Lark.")

    print("\n--- BƯỚC 3: PHÂN LOẠI & CẬP NHẬT/TẠO MỚI ---")
    moms_to_create = []
    records_to_update = []

    for lark_record in lark_roots:
        main_id = lark_record.get("main_id")
        if main_id in pancake_data_map:
            pos_order = pancake_data_map[main_id]
            fields_to_apply = pos_order["fields"]
            lark_fields = lark_record["fields"]

            # Trên Lark đã có thông tin công ty (thường là tự điền tay)?
            lark_has_company = bool(extract_text(lark_fields.get("MST")) or extract_text(lark_fields.get("Tên đơn vị")))

            update_payload = {}
            for key, val in fields_to_apply.items():
                new_val = str(val or "").strip()

                # 1. Không bao giờ ghi ô trống đè lên dữ liệu trên Lark
                if not new_val: continue

                # 2. POS không có thông tin công ty nhưng Lark đã có -> giữ nguyên cụm thông tin công ty trên Lark
                if not pos_order["has_company"] and lark_has_company and key in COMPANY_FIELDS: continue

                current_val = extract_text(lark_fields.get(key))
                if str(current_val) != new_val:
                    update_payload[key] = new_val

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
    updated_count = batch_execute_lark(headers, records_url, records_to_update, action="update") if records_to_update else 0
    added_moms_count = batch_execute_lark(headers, records_url, moms_to_create, action="create") if moms_to_create else 0

    print("\n--- BƯỚC 4: KIỂM TRA & TẠO BẢN GHI CON THIẾU ---")
    fresh_items = search_lark_records_by_ids(headers, records_url, system_ids_to_fetch)
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

    added_subs_count = batch_execute_lark(headers, records_url, subs_to_create, action="create") if subs_to_create else 0

    print(f"-> Thêm con: {added_subs_count}")

    xoa_me, xoa_con = don_dep_toan_bang(headers, records_url)
    print(f"\n✅ HOÀN TẤT TOÀN BỘ! Thêm mẹ: {added_moms_count} | Cập nhật dòng: {updated_count} | Thêm con: {added_subs_count} | Xóa mẹ trùng: {xoa_me} | Xóa con thừa: {xoa_con}.")


if __name__ == "__main__":
    main()
