# DEME Automation

Tự động lấy số liệu từ Pancake (hội thoại, lead, đơn hàng) và Meta (tiếp cận Facebook/Instagram), ghi vào các bảng theo dõi trên Lark Base. Chạy bằng GitHub Actions.

> Repo này để **public**. Không commit file CSV, file `.env`, `token_store.json` hay bất kỳ token, API key nào. Mọi thông tin nhạy cảm nằm trong **GitHub Secrets**. Log của các lần chạy cũng công khai: không thêm lệnh in ra token, số điện thoại khách hay số liệu theo tên nhân viên.

## Danh sách workflow

Mỗi file code nằm ở thư mục gốc, file workflow cùng tên nằm trong `.github/workflows/`.

| File code | Workflow (.yml) | Chạy khi nào | Việc làm |
|---|---|---|---|
| `hoi_thoai_theo_kenh.py` | `hoi_thoai_theo_kenh.yml` | Hằng ngày (lịch GitHub) | Ghi Tổng hội thoại và Quảng cáo ngày hôm qua của tất cả kênh Pancake |
| `lead_kenh_ecom.py` | `lead_kenh_ecom.yml` | Hằng ngày (lịch GitHub) | Ghi lead ngày hôm qua của các kênh Shopee / TikTok Shop |
| `organic_chat_mkt_fb_ig.py` | `organic_chat_mkt_fb_ig.yml` | Hằng ngày (lịch GitHub) | Ghi hội thoại tự nhiên (Organic Chat) ngày hôm qua của FB DU, IG DU, DCF |
| `lead_tung_kenh_theo_thang.py` | `lead_tung_kenh_theo_thang.yml` | Hằng ngày (lịch GitHub) | Ghi lead lũy kế từ mùng 1 đến hôm qua của từng kênh, mỗi tháng 1 dòng/kênh |
| `lead_meta_ads_fb_mkt.py sync` | `lead_meta_ads_fb_mkt.yml` | Hằng ngày (lịch GitHub) | Ghi tiếp cận organic / trả phí 15 ngày gần nhất của 4 kênh Facebook, Instagram |
| `lead_meta_ads_fb_mkt.py refresh` | `refresh_token_fb.yml` | Hằng tháng (lịch GitHub) | Đổi token Meta mới, lưu lại vào secret `LONG_LIVED_TOKEN` |
| `theo_doi_hieu_suat_sale_realtime.py` | `theo_doi_hieu_suat_sale_realtime.yml` | 30 phút/lần (cron-job.org) | Ghi lead, khách cũ, số hội thoại chưa phản hồi theo từng sale |
| `update_check_zalo_ecom.py` | `update_check_zalo_ecom.yml` | cron-job.org | Cập nhật Thẻ Pancake (C1, C2, Zalo...) cho các đơn tạo trong 15 ngày gần nhất |
| `dien_don_xuat_vat_tu_dong.py` | `dien_don_xuat_vat_tu_dong.yml` | cron-job.org | Đồng bộ đơn hàng Pancake POS cập nhật trong 5 ngày gần nhất (dòng mẹ / con theo sản phẩm) |
| `dien_don_xuat_vat_ke_toan.py` | `dien_don_xuat_vat_ke_toan.yml` | cron-job.org | Đẩy đơn có thẻ VAT đã giao thành công (3 ngày gần nhất) vào bảng Kế toán, mỗi sản phẩm 1 dòng |

Tất cả workflow đều bấm chạy tay được: tab **Actions** → chọn workflow → **Run workflow**.

### Lịch chạy

- **Lịch GitHub** (dòng `cron` trong file yml) tính theo giờ UTC, tức giờ Việt Nam trừ 7 tiếng. GitHub thường chạy trễ vài tiếng, đôi khi bỏ lượt, nên các workflow hằng ngày được đặt lịch sớm để thực tế chạy khoảng 6–7h sáng.
- Lịch hằng ngày phải đặt **sau 00:00 giờ Việt Nam** (sau `17:00` UTC). Các script lấy số liệu "hôm qua" theo giờ chạy thực tế, chạy trước nửa đêm sẽ lấy nhầm ngày.
- Các workflow cùng gọi Pancake nên đặt lệch nhau 15–30 phút để tránh lỗi 429 (gọi quá nhiều).
- Đến giữa buổi sáng chưa thấy số liệu hôm qua trên Lark thì bấm **Run workflow** để chạy tay.

## Bảng Lark mà từng file ghi dữ liệu

| File code | Base | Bảng | Cách khớp dòng | Tự tạo dòng? |
|---|---|---|---|---|
| `hoi_thoai_theo_kenh.py` | SALE - Customer data | Báo cáo hội thoại theo từng kênh | `Ngày` + `Nền tảng` + `Tên kênh` | Có |
| `lead_kenh_ecom.py` | SALE - Customer data | Báo cáo ecom tổng hợp từng kênh | `Ngày` + `Kênh` | Có |
| `theo_doi_hieu_suat_sale_realtime.py` | SALE - Customer data | Theo dõi chia leads theo sales | `Tên TK POS` = tên tài khoản sale trên Pancake | Không |
| `update_check_zalo_ecom.py` | SALE - Customer data | POS | `Mã đơn hàng đầy đủ` (dạng `S<mã shop>O<mã đơn>`) | Không (mặc định) |
| `dien_don_xuat_vat_tu_dong.py` | SALE - Customer data | Bảng đơn hàng | `ID` = mã đơn Pancake | Có, tự xoá dòng trùng |
| `dien_don_xuat_vat_ke_toan.py` | Kế toán | Kế toán 2026 | `ID` = mã đơn Pancake | Chỉ thêm đơn mới, đơn đã có thì bỏ qua |
| `lead_tung_kenh_theo_thang.py` | Base riêng | Lead theo tháng | `Tháng` (dạng `9.2026`) + `Nền tảng` + `Tên kênh` | Có (tháng hiện tại) |
| `organic_chat_mkt_fb_ig.py` | Base riêng | Organic Chat | `Ngày` + `Tên kênh` (FB DU, IG DU, DCF) | Có |
| `lead_meta_ads_fb_mkt.py` | Base Meta | 4 bảng: FB DEME, FB DEME Uniform, FB Đồng Phục Cafe, IG DEME Uniform | `Ngày` | Có |

Quy tắc chung:

- "Tự tạo dòng": chưa có dòng thì tạo mới; đã có thì chỉ cập nhật số liệu, không tạo trùng. Chạy lại nhiều lần không sinh dòng thừa.
- Gọi Pancake / Meta bị lỗi thì giữ nguyên số cũ, không ghi đè bằng 0, và workflow báo đỏ.
- Tên cột và tên kênh trên Lark phải **khớp từng ký tự** với tên trong code / CSV. Lệch tên thì script coi như chưa có dòng và tạo dòng mới. **Đổi tên cột trên Lark thì phải sửa tên tương ứng ở đầu file code** (các biến `FIELD_...`, `COL_...`).

### Mã base và mã bảng

Lấy từ link bảng trên trình duyệt:

```
https://<tên miền>.larksuite.com/base/<APP TOKEN>?table=<TABLE ID>&view=...
```

| File code | Mã base (app token) | Mã bảng (table ID) |
|---|---|---|
| `hoi_thoai_theo_kenh.py`, `lead_kenh_ecom.py`, `theo_doi_hieu_suat_sale_realtime.py`, `dien_don_xuat_vat_tu_dong.py` | Secret `LARK_BASE_APP_TOKEN` | Ghi sẵn ở đầu file |
| `dien_don_xuat_vat_ke_toan.py` | Secret `LARK_BASE_APP_TOKEN_KE_TOAN` | Ghi sẵn ở đầu file |
| `update_check_zalo_ecom.py`, `lead_tung_kenh_theo_thang.py`, `organic_chat_mkt_fb_ig.py`, `lead_meta_ads_fb_mkt.py` | Ghi sẵn ở đầu file | Ghi sẵn ở đầu file |

Mã base / mã bảng thay đổi khi bảng được tạo lại, nhân bản hoặc chuyển sang base khác. Khi đó sửa ở đầu file code hoặc cập nhật secret tương ứng.

**Không dán link bảng Lark vào README hay ghi chú trong code**: link chứa mã base, mà repo này public.

App Lark (theo `LARK_APP_ID`) phải được thêm vào **từng base** với quyền **Có thể chỉnh sửa** (base → `...` → More → Add document app). Base nào bật Quyền nâng cao thì app phải thuộc vai trò được xem, sửa và thêm bản ghi trên các bảng liên quan. Thiếu quyền sẽ gặp lỗi `RolePermNotAllow` hoặc `91402`.

## GitHub Secrets

Vào **Settings → Secrets and variables → Actions**. GitHub không cho xem lại nội dung secret sau khi lưu, nên **giữ bản gốc của CSV và các key ở nơi riêng tư** (máy cá nhân hoặc thư mục giới hạn quyền). Mọi thay đổi đều sửa bản gốc trước, rồi mới cập nhật secret.

| Secret | Nội dung | Dùng cho |
|---|---|---|
| `LARK_APP_ID` | App ID của app Lark | Tất cả |
| `LARK_APP_SECRET` | App Secret của app Lark | Tất cả |
| `LARK_BASE_APP_TOKEN` | Mã base SALE - Customer data | Hội thoại, Lead ecom, Realtime, Đơn hàng |
| `LARK_BASE_APP_TOKEN_KE_TOAN` | Mã base Kế toán | VAT kế toán |
| `PAGES_CONFIG_CSV_B64` | File `pages_config_full.csv` mã hoá base64 | Hội thoại, Lead ecom, Organic chat, Realtime |
| `PAGES_CONFIG_CSV_2_B64` | File `pages_config_full_2.csv` mã hoá base64 | Lead theo tháng |
| `PERSONAL_ZALO_PAGE_IDS` | Page ID Zalo cá nhân của sale, nối bằng dấu phẩy, không xuống dòng | Realtime |
| `PANCAKE_API_KEY` | API key Pancake POS shop chính (đọc được toàn bộ đơn và thông tin khách) | Đơn hàng, VAT kế toán |
| `PANCAKE_SHOP_ID` | Mã shop chính trên Pancake POS | Đơn hàng, VAT kế toán |
| `PANCAKE_API_KEY_1890165155` | API key Pancake POS shop 1890165155 | Update check Zalo ecom |
| `LONG_LIVED_TOKEN` | Token Meta dài hạn (workflow refresh tự cập nhật hằng tháng) | Meta |
| `META_APP_SECRET` | App Secret của app Meta | Refresh token Meta |
| `GH_TOKEN` | Token GitHub fine-grained, quyền **Secrets: Read and write** trên repo này | Refresh token Meta |

Tên secret trong file yml (phần `env:`) phải khớp đúng tên biến mà code đọc. Thiếu biến thì script dừng ngay và báo thiếu biến nào.

## File CSV cấu hình kênh

Hai file CSV **không có trong repo**. Mỗi lần chạy, workflow giải mã từ secret ra file tạm ở thư mục gốc.

**`pages_config_full.csv`**, các cột `platform,channel,page_id,access_token`:

- `platform`: viết thường, ví dụ `facebook`, `instagram`, `zalo`, `shopee`, `tiktok`, `tiktokshop`.
- `channel`: tên kênh. Tên này được ghi lên Lark và dùng để khớp dòng, nên **đặt đúng ngay từ đầu, không đổi về sau**. Đổi tên sẽ làm kênh tách thành 2 dòng khác nhau trên Lark.
- `access_token`: token của page trên Pancake. Để trống thì kênh vẫn có dòng trên Lark nhưng không có số liệu.

**`pages_config_full_2.csv`** (chỉ cho lead theo tháng): giống file trên, thêm 3 cột:

- `lark_platform`, `lark_channel`: Nền tảng và Tên kênh của dòng trên bảng lead theo tháng. Nhiều page cùng khai báo một kênh thì lead được cộng dồn. Để trống thì cộng vào dòng "Không xác định".
- `lark_project`: Dự án, dùng khi tạo dòng mới. Để trống thì lấy Dự án của kênh đó ở các tháng trước.

### Mã hoá CSV và cập nhật secret

Mở PowerShell (hoặc Terminal trong VS Code) tại thư mục chứa file CSV:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("pages_config_full.csv")) | Set-Clipboard
```

Vào GitHub → bấm vào secret `PAGES_CONFIG_CSV_B64` → **Update** → dán → lưu. Với file `pages_config_full_2.csv`, đổi tên file trong lệnh và cập nhật secret `PAGES_CONFIG_CSV_2_B64`.

Không dùng các trang mã hoá base64 trên mạng.

## Thay đổi thường gặp

### Thêm kênh Pancake mới

1. Lấy `page_id` và `access_token` của kênh trên Pancake.
2. Thêm 1 dòng vào `pages_config_full.csv` gốc, mã hoá lại và cập nhật secret `PAGES_CONFIG_CSV_B64`.
3. Từ lần chạy sau, bảng hội thoại tự tạo dòng cho kênh mới, workflow Realtime tự quét kênh mới.
4. Cần có trong bảng lead theo tháng: thêm dòng vào `pages_config_full_2.csv` (điền `lark_platform`, `lark_channel`, `lark_project`), cập nhật secret `PAGES_CONFIG_CSV_2_B64`.
5. Cần có trong bảng Lead ecom: thêm 1 dòng vào `DAILY_ROWS` ở đầu `lead_kenh_ecom.py`.
6. Cần có trong bảng Organic Chat: thêm 1 dòng vào `KENH` ở đầu `organic_chat_mkt_fb_ig.py` (tên kênh phải trùng lựa chọn của cột "Tên kênh" trên Lark).

### Thêm kênh Meta (Facebook / Instagram)

Thêm 1 dòng vào `KENH` ở đầu `lead_meta_ads_fb_mkt.py`: tên hiển thị, loại `fb`/`ig`, Facebook Page ID, table ID trên Lark, tên cột organic. Token Meta phải có quyền quản lý page đó.

### Thêm sale mới

1. Thêm tài khoản sale vào các page trên Pancake.
2. Thêm 1 dòng vào bảng **Theo dõi chia leads theo sales**, cột **Tên TK POS** ghi đúng tên tài khoản Pancake (không phân biệt có dấu / không dấu, hoa / thường). Script không tự tạo dòng cho sale.
3. Sale có Zalo cá nhân kết nối Pancake: thêm kênh Zalo đó vào CSV, và thêm page ID (dạng `pzl_...`) vào cuối secret `PERSONAL_ZALO_PAGE_IDS`. Thiếu bước này thì lead Zalo đó bị tính vào kênh chung.

### Sale nghỉ việc

1. Xoá dòng kênh Zalo cá nhân của sale khỏi CSV, cập nhật secret.
2. Xoá page ID khỏi `PERSONAL_ZALO_PAGE_IDS`.
3. Dòng của sale trên bảng Lark: xoá hoặc giữ lại tuỳ nhu cầu lưu số liệu cũ.

### Bảng tóm tắt

| Thay đổi | CSV + secret | `PERSONAL_ZALO_PAGE_IDS` | Bảng Lark | Code |
|---|---|---|---|---|
| Thêm kênh Pancake | `pages_config_full.csv` | | | |
| Thêm kênh vào lead theo tháng | `pages_config_full_2.csv` | | | |
| Thêm kênh vào Lead ecom | Có | | | Sửa `DAILY_ROWS` |
| Thêm kênh vào Organic Chat | Có | | Thêm lựa chọn cột Tên kênh | Sửa `KENH` |
| Thêm kênh Meta | | | Tạo bảng | Sửa `KENH` |
| Thêm sale | | | Thêm dòng Tên TK POS | |
| Thêm Zalo cá nhân của sale | Có | Thêm ID | | |
| Sale nghỉ việc | Xoá dòng | Xoá ID | Tuỳ chọn | |

Sau mỗi thay đổi, bấm **Run workflow** để chạy thử ngay.

## Chạy tay và chạy bù

- Chạy ngay: tab **Actions** → chọn workflow → **Run workflow**.
- Hội thoại, Lead ecom, Organic chat: nhập ô **TỪ ngày** / **ĐẾN ngày** dạng `dd/mm/yyyy`. Chỉ nhập "TỪ ngày" thì chạy đến hôm qua. Để trống thì chạy cho hôm qua.
- Lead theo tháng: nhập ô `backfill_from` dạng `M.YYYY` (ví dụ `1.2026`), chạy từ tháng đó đến tháng hiện tại. Đặt `dry_run = true` để chạy thử, chỉ in log.
- Update check Zalo ecom: nhập số ngày, cột cần cập nhật (`all` = tất cả), `dry_run`.
- Chạy bù sẽ **ghi đè** số liệu đã có trong khoảng đó bằng số mới lấy từ nguồn.
- Sau khi sửa code, luôn bấm **Run workflow**. Không dùng **Re-run jobs**: nút này chạy lại đúng phiên bản code cũ.

## Kích hoạt bằng cron-job.org

Các workflow chạy theo cron-job.org không dùng lịch GitHub (vì GitHub hay trễ và bỏ lượt). Mỗi workflow có 1 cronjob riêng, gọi GitHub API giống như bấm Run workflow.

**Token GitHub cho cron-job.org**: GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token:

- Repository access: **Only select repositories**, chọn repo này.
- Permissions: **Actions: Read and write**.
- Expiration: đặt thời hạn (ví dụ 1 năm) và ghi lại ngày hết hạn.

Nên tách riêng token này với `GH_TOKEN` (dùng để ghi secret).

**Cấu hình cronjob**:

| Mục | Giá trị |
|---|---|
| URL | `https://api.github.com/repos/<tài khoản>/<tên repo>/actions/workflows/<tên file yml>/dispatches` |
| Request method | `POST` |
| Header `Authorization` | `Bearer github_pat_...` |
| Header `Accept` | `application/vnd.github+json` |
| Header `X-GitHub-Api-Version` | `2022-11-28` |
| Header `Content-Type` | `application/json` |
| Request body | `{"ref":"main"}` |
| Timeout | Mức cao nhất cho phép |

Thêm cronjob mới: nhân bản (Clone) một cronjob đang chạy, chỉ sửa tên và tên file yml trong URL.

Bấm **Test run**: mã **204** là thành công.

| Mã | Nguyên nhân |
|---|---|
| 401 | Token sai, hết hạn hoặc dán thiếu |
| 403 | Token thiếu quyền Actions: Read and write |
| 404 | Sai tên tài khoản / repo / file yml, hoặc token không được cấp quyền cho repo này |
| 422 | Sai tên nhánh trong body, hoặc file yml không có `workflow_dispatch` |
| Failed (timeout) | GitHub trả lời chậm. Thường workflow vẫn đã chạy, kiểm tra tab Actions |

**Đổi tên file yml hoặc đổi tên repo thì phải sửa URL trên cron-job.org.** Bật thông báo lỗi khi cronjob thất bại nhiều lần liên tiếp.

## Đọc log và xử lý lỗi

| Thông báo | Nguyên nhân | Cách xử lý |
|---|---|---|
| `Thiếu biến môi trường: ...` | Chưa tạo secret, hoặc tên trong `env:` của yml không khớp tên biến trong code | Kiểm tra tên secret và phần `env:` |
| `No such file or directory` | Dòng `run:` trong yml sai tên file `.py` | Sửa dòng `run:` đúng tên file ở thư mục gốc |
| `Extra data: line 1 column 5` / `Lark trả HTTP 404` | Mã base rỗng hoặc sai | Kiểm tra secret mã base |
| `code: 91402, NOTEXIST` | Base hoặc bảng không tồn tại với mã đã cấu hình | Kiểm tra mã base, mã bảng |
| `code: 1254302, RolePermNotAllow` | App Lark chưa có quyền trên base | Thêm app vào base với quyền chỉnh sửa |
| `code: 10014, app secret invalid` | App Secret Lark sai hoặc đã đổi | Cập nhật `LARK_APP_SECRET` |
| `401 Unauthorized` / `Pancake 4xx` ở một kênh | Token page hết hạn hoặc sai | Lấy lại token trên Pancake, cập nhật CSV và secret |
| `Lỗi lấy page token` / Meta báo token hết hạn | `LONG_LIVED_TOKEN` hết hạn | Chạy workflow refresh; nếu token đã hết hạn hẳn, tạo token Meta mới |
| Số dòng "tạo mới" nhiều hơn dự kiến | Tên kênh trên Lark lệch với CSV / code | Sửa tên cho khớp, xoá dòng trùng |
| `N nhân viên Pancake không có trong cột 'Tên TK POS'` | Sale chưa có dòng trên Lark hoặc tên lệch | Thêm / sửa dòng trên Lark |

Workflow Realtime mặc định không in số liệu theo tên sale ra log. Khi cần dò lỗi, thêm `LOG_THEO_SALE: '1'` vào phần `env:` của bước chạy script, dò xong thì xoá.

## Sửa code bằng VS Code

1. Luôn chạy `git pull` **trước khi bắt đầu sửa**. Hạn chế vừa sửa trên web GitHub vừa sửa trên máy.
2. Sửa xong, mở **Source Control** (`Ctrl + Shift + G`) kiểm tra danh sách file. **Không được có `.csv`, `.env`, `token_store.json`.**
3. Commit, rồi **Sync Changes** (hoặc `git push`).
4. Vào tab Actions bấm **Run workflow** để kiểm tra.

Gặp lỗi "Your local changes ... would be overwritten by merge" khi pull: commit thay đổi trên máy trước, rồi `git pull --no-rebase --no-edit -X ours` (giữ bản trên máy ở chỗ trùng), sau đó `git push`. Không dùng `git push --force`.

## Lưu ý vận hành

- **Token Meta hết hạn sau khoảng 60 ngày.** Workflow refresh chạy mỗi tháng theo lịch GitHub; nếu bị bỏ lượt hoặc bị tắt, toàn bộ dữ liệu Meta ngừng cập nhật. Nên kích hoạt thêm bằng cron-job.org, hoặc đầu mỗi tháng kiểm tra tab Actions.
- Repo public **60 ngày không có commit mới**, GitHub tự tắt các workflow chạy theo lịch GitHub (có email báo trước). Nhận email thì vào tab Actions bật lại. Workflow chạy qua cron-job.org không bị ảnh hưởng.
- Token GitHub (`GH_TOKEN`, token cron-job.org) có hạn dùng. Đặt nhắc lịch gia hạn trước ngày hết hạn.
- Nếu lỡ commit token / key: xoá khỏi code chưa đủ vì lịch sử commit vẫn lưu. Phải **đổi ngay** key đó trên Lark / Pancake / Meta, rồi cập nhật secret.

## Hệ thống liên quan (ngoài repo này)

**Fill-Data-KH** (Node.js, chạy trên Render, repo riêng): webhook nhận yêu cầu từ Lark Automation, tra cứu đơn Pancake theo mã đơn và điền tên khách, SĐT, nguồn đơn, AD_ID vào Lark. Key và cấu hình nằm ở phần **Environment** của service trên Render. Khi đổi API key Pancake hoặc App Secret Lark, nhớ cập nhật cả ở Render.

Render gói free: 750 giờ chạy / tháng cho cả workspace, service tự ngủ sau 15 phút không có request. Theo dõi ở Render → Billing → **Included Usage**.

## Chạy trên máy cá nhân

```powershell
pip install -r requirements.txt

# Khai báo biến cho phiên PowerShell hiện tại (chỉ khai báo biến mà script cần)
$env:LARK_APP_ID = "..."
$env:LARK_APP_SECRET = "..."
$env:LARK_BASE_APP_TOKEN = "..."

# Đặt file pages_config_full.csv ở thư mục gốc, rồi chạy
python hoi_thoai_theo_kenh.py
```

Script nào có chế độ chạy thử (`dry_run`, `--dry-run`, `DRY_RUN`) thì nên chạy thử trước. `theo_doi_hieu_suat_sale_realtime.py` và `dien_don_xuat_vat_ke_toan.py` còn tự đọc file `.env` ở thư mục gốc nếu có. File `.env` đã bị chặn trong `.gitignore`.