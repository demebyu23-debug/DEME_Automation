# Đồng bộ Pancake → Lark Base (DEME)

Tự động lấy số liệu hội thoại và lead từ Pancake, ghi vào các bảng theo dõi trên Lark Base. Chạy bằng GitHub Actions.

> Repo này để **public**. Không commit file CSV, file `.env` hay bất kỳ token, mã định danh nào. Mọi thông tin nhạy cảm đều nằm trong GitHub Secrets.

## Các workflow

| Workflow | File code | Lịch chạy | Việc làm |
|---|---|---|---|
| Daily Pancake Chat Stats Sync | `pancake_to_lark.py` | 1 lần/ngày, thực tế chạy khoảng 7h sáng | Ghi Tổng hội thoại và Quảng cáo của ngày hôm qua cho tất cả kênh trong CSV vào bảng Chat |
| Daily Pancake Lead Ecom | `Extract_Lead_Ecom.py` | 1 lần/ngày, thực tế chạy khoảng 7h sáng | Ghi lead ngày hôm qua của các kênh Shopee/TikTok Shop vào bảng Lead Ecom |
| Realtime Follow Lead (30 mins) | `Follow_lead_realtime.py` | 30 phút/lần, do cron-job.org kích hoạt | Ghi lead, khách cũ, số hội thoại chưa phản hồi theo từng sale vào bảng theo dõi chia lead |
| Auto Sync Pancake Orders to Lark | `sync_pancake_orders.py` | Do cron-job.org kích hoạt | Đồng bộ đơn hàng Pancake POS cập nhật trong 5 ngày gần nhất vào bảng đơn hàng (dòng mẹ/con), dọn dòng trùng |
| Push VAT Ke Toan | `Push_Vat_Ke_Toan.py` | Do cron-job.org kích hoạt | Đẩy đơn có thẻ VAT đã giao thành công (3 ngày gần nhất) vào bảng Kế toán, mỗi sản phẩm 1 dòng |
| Daily Pancake Lead to Lark (theo tháng) | `daily_lead.py` | 06:00 hằng ngày (lịch GitHub) | Ghi tổng lead lũy kế từ mùng 1 đến hôm qua của từng kênh vào bảng lead theo tháng |

Lịch của 2 workflow hằng ngày (dòng `cron` trong file yml) tính theo giờ UTC, tức giờ Việt Nam trừ 7 tiếng.

GitHub thường chạy trễ so với lịch, có khi trễ vài tiếng, đôi khi bỏ qua hẳn một lượt. Vì vậy 2 workflow hằng ngày được đặt lịch sớm để thực tế chạy vào khoảng 7h sáng. Khi chỉnh lịch cần lưu ý:

- Lịch phải sau **00:00 giờ Việt Nam** (tức sau `17:00` UTC). Script lấy số liệu của "hôm qua" theo giờ chạy thực tế; nếu chạy trước nửa đêm sẽ lấy nhầm ngày.
- Nếu đến giữa buổi sáng chưa thấy số liệu hôm qua trên Lark, vào tab Actions bấm **Run workflow** để chạy tay.
- 2 workflow hằng ngày nên đặt lệch nhau ít nhất 15–30 phút để không gọi Pancake cùng lúc.

### Bảng Lark mà từng file code ghi dữ liệu

Các bảng nằm trong base **SALE - Customer data**, riêng bảng Kế toán 2026 nằm ở base Kế toán và bảng lead theo tháng nằm ở một base riêng.

| File code | Bảng Lark | Cách khớp dòng | Cột được ghi |
|---|---|---|---|
| `Follow_lead_realtime.py` | **Theo dõi chia leads theo sales** | Cột `Tên TK POS` = tên tài khoản sale trên Pancake | `LEAD Zalo cá nhân`, `LEAD kênh chung`, `KH cũ zalo cá nhân`, `KH cũ kênh chung`, `Số hội thoại chưa phản hồi` |
| `pancake_to_lark.py` | **Báo cáo hội thoại theo từng kênh** | `Ngày` + `Nền tảng` + `Tên kênh` | `Ngày`, `Nền tảng`, `Tên kênh` (khi tạo dòng mới), `Tổng hội thoại`, `Quảng cáo` |
| `Extract_Lead_Ecom.py` | **Báo cáo ecom tổng hợp từng kênh** | `Ngày` + `Kênh` | `Ngày`, `Kênh` (khi tạo dòng mới), `Lead` |
| `sync_pancake_orders.py` | Bảng đơn hàng (base SALE - Customer data) | Cột `ID` = mã đơn Pancake | Thông tin đơn, dòng mẹ/con theo sản phẩm; tự xoá dòng mẹ trùng và dòng con thừa |
| `Push_Vat_Ke_Toan.py` | **Kế toán 2026** (base Kế toán, **khác base** với các bảng trên) | Cột `ID` = mã đơn Pancake | Chỉ thêm đơn mới, đơn đã có ID trên bảng thì bỏ qua, không sửa |
| `daily_lead.py` | Bảng lead theo tháng (base riêng, **khác base** SALE - Customer data) | `Tháng` (dạng `9.2026`) + `Nền tảng` + `Tên kênh` (không phân biệt dấu, hoa/thường) | `Tháng`, `Dự án`, `Nền tảng`, `Tên kênh` (khi tạo dòng mới), `Lead` |

Tên cột trong bảng phải khớp đúng với tên khai báo ở đầu mỗi file code (các biến `FIELD_...`). **Đổi tên cột trên Lark thì phải sửa tên tương ứng trong code**, nếu không script sẽ báo lỗi hoặc ghi không vào.

#### Mã base (app token) và mã bảng (table ID)

Mã base và mã bảng **có thể khác nhau** giữa các bảng, và sẽ thay đổi nếu bảng hoặc base được tạo lại, nhân bản hay chuyển sang base khác. Khi đó phải cập nhật lại cấu hình.

Cách lấy mã: mở bảng trên trình duyệt, xem link có dạng

```
https://<tên miền>.larksuite.com/base/<APP TOKEN>?table=<TABLE ID>&view=...
```

- `APP TOKEN`: mã base. Các bảng thuộc base SALE - Customer data dùng chung secret `LARK_BASE_APP_TOKEN`; bảng Kế toán 2026 dùng secret riêng `LARK_BASE_APP_TOKEN_KE_TOAN`; riêng `daily_lead.py` ghi sẵn mã base trong code (biến `LARK_BASE_APP_TOKEN` ở đầu file). Nếu sau này một bảng chuyển sang base khác, cần tạo secret riêng cho file code đó.
- `TABLE ID` (dạng `tbl...`): mã bảng. Chỗ khai báo trong từng file:

| File code | Chỗ khai báo table ID |
|---|---|
| `Follow_lead_realtime.py` | Biến môi trường `LARK_TABLE_ID_SALE`; không đặt thì dùng mã mặc định ghi trong code (biến `LARK_TABLE_ID`) |
| `pancake_to_lark.py` | Biến `LARK_TABLE_ID` ở đầu file |
| `Extract_Lead_Ecom.py` | Biến môi trường `LARK_TABLE_ID_LEAD`; không đặt thì dùng mã mặc định ghi trong code (biến `LARK_TABLE_ID`) |
| `sync_pancake_orders.py` | Biến môi trường `LARK_TABLE_ID_ORDERS`; không đặt thì dùng mã mặc định ghi trong code (biến `LARK_TABLE_ID`) |
| `Push_Vat_Ke_Toan.py` | Biến môi trường `LARK_TABLE_ID_KE_TOAN`; không đặt thì dùng mã mặc định ghi trong code (biến `LARK_TABLE_ID`) |
| `daily_lead.py` | Ghi thẳng trong code (biến `LARK_TABLE_ID` ở đầu file, cạnh mã base). Bảng đổi base hoặc tạo lại thì sửa 2 dòng này |

Không dán link bảng Lark vào README hay code: link chứa mã base, mà repo này để public.

App Lark (theo `LARK_APP_ID`) phải được thêm vào base với quyền **chỉnh sửa**, nếu không sẽ gặp lỗi quyền truy cập khi đọc hoặc ghi bảng.

### Lịch chạy Realtime bằng cron-job.org

Các workflow Realtime, Auto Sync Pancake Orders và Push VAT Ke Toan **không dùng lịch của GitHub** (vì hay trễ và bỏ lượt). Mỗi workflow cần 1 cronjob riêng trên cron-job.org, URL khác nhau ở tên file yml. Thay vào đó, dịch vụ cron-job.org gọi GitHub API mỗi 30 phút để kích hoạt workflow, giống như bấm nút Run workflow.

**1. Tạo token GitHub (chỉ cho phép chạy workflow của repo này)**

GitHub → ảnh đại diện → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**:

- Repository access: **Only select repositories**, chọn đúng repo này.
- Permissions → Repository permissions → **Actions: Read and write**. Không cấp quyền nào khác.
- Expiration: chọn thời hạn (ví dụ 1 năm) và ghi lại ngày hết hạn. Hết hạn thì workflow ngừng chạy mà không báo lỗi trên GitHub.

Copy token (dạng `github_pat_...`). Token chỉ hiện một lần.

**2. Tạo cronjob trên cron-job.org**

| Mục | Giá trị |
|---|---|
| URL | `https://api.github.com/repos/<tên tài khoản>/<tên repo>/actions/workflows/<tên file yml Realtime>/dispatches` |
| Lịch | Mỗi 30 phút (có thể giới hạn trong giờ làm việc), múi giờ Asia/Ho_Chi_Minh |
| Request method | `POST` |
| Header `Authorization` | `Bearer github_pat_...` |
| Header `Accept` | `application/vnd.github+json` |
| Header `X-GitHub-Api-Version` | `2022-11-28` |
| Header `Content-Type` | `application/json` |
| Request body | `{"ref":"main"}` (đổi `main` nếu nhánh chính tên khác) |

Tên file yml là tên file trong `.github/workflows/`, ví dụ `realtime_follow_lead.yml`.

Bấm **Test run** trên cron-job.org: kết quả **204** là thành công, trên tab Actions sẽ thấy một lượt chạy mới.

| Mã lỗi | Nguyên nhân |
|---|---|
| 401 | Token sai hoặc đã hết hạn |
| 403 | Token thiếu quyền Actions: Read and write |
| 404 | Sai tên tài khoản, tên repo hoặc tên file yml; hoặc token không được cấp quyền cho repo này |
| 422 | Sai tên nhánh trong body, hoặc file yml chưa có `workflow_dispatch` |

**Bảo mật:** token nằm trên cron-job.org, không đưa vào repo. Nếu nghi lộ token, vào GitHub xoá token cũ, tạo token mới và cập nhật header trên cron-job.org.

### Cách ghi dữ liệu

- **Bảng Chat và bảng Lead Ecom**: mỗi ngày, kênh nào đã có dòng thì chỉ cập nhật số liệu; kênh nào chưa có dòng thì tự tạo dòng mới (điền Ngày, Nền tảng/Kênh và số liệu). Chạy lại nhiều lần không tạo dòng trùng.
- **Bảng theo dõi chia lead theo sales**: chỉ cập nhật các dòng có sẵn, khớp theo cột **Tên TK POS**. Script không tự tạo dòng cho sale.
- Bảng Lead Ecom có danh sách dòng cố định khai báo trong `DAILY_ROWS` ở đầu file `Extract_Lead_Ecom.py`. Dòng "Không xác định" luôn điền 0.
- **Bảng lead theo tháng** (`daily_lead.py`): mỗi tháng mỗi kênh 1 dòng. Script tự tạo dòng còn thiếu cho tháng hiện tại theo danh sách kênh trong `pages_config_full_2.csv`, rồi ghi số lead lũy kế của cả tháng (mùng 1 đến hôm qua). Ngày mùng 1 sẽ chốt số đủ của tháng trước. Page trong CSV không khai báo kênh Lark được cộng dồn vào dòng "Không xác định". Kênh gọi Pancake lỗi thì giữ nguyên số cũ, không ghi đè.

## Cấu trúc repo

```
├── .github/workflows/      # 6 file workflow
├── pancake_to_lark.py
├── Extract_Lead_Ecom.py
├── Follow_lead_realtime.py
├── sync_pancake_orders.py
├── Push_Vat_Ke_Toan.py
├── daily_lead.py
├── requirements.txt
├── .gitignore              # chặn *.csv, .env
└── README.md
```

File `pages_config_full.csv` và `pages_config_full_2.csv` **không có trong repo**. Mỗi lần chạy, workflow giải mã từ secret ra file tạm.

## GitHub Secrets cần có

Vào **Settings → Secrets and variables → Actions**.

| Secret | Nội dung |
|---|---|
| `LARK_APP_ID` | App ID của app Lark |
| `LARK_APP_SECRET` | App Secret của app Lark |
| `LARK_BASE_APP_TOKEN` | Mã base Lark (lấy trong link base: `.../base/<mã này>?table=...`) |
| `PAGES_CONFIG_CSV_B64` | File `pages_config_full.csv` đã mã hoá base64 |
| `PAGES_CONFIG_CSV_2_B64` | File `pages_config_full_2.csv` đã mã hoá base64 (chỉ dùng cho `daily_lead.py`) |
| `PERSONAL_ZALO_PAGE_IDS` | Danh sách page ID Zalo cá nhân của sale, nối bằng dấu phẩy, không xuống dòng |
| `PANCAKE_API_KEY` | API key của shop trên Pancake POS (dùng cho 2 file đơn hàng và VAT). Key này đọc được toàn bộ đơn và thông tin khách |
| `PANCAKE_SHOP_ID` | Mã shop trên Pancake POS |
| `LARK_BASE_APP_TOKEN_KE_TOAN` | Mã base Kế toán (chứa bảng Kế toán 2026) |

GitHub không cho xem lại nội dung secret sau khi lưu. **Luôn giữ bản gốc của cả 2 file CSV và danh sách ID Zalo cá nhân ở nơi riêng tư** (máy cá nhân hoặc thư mục giới hạn quyền). Mọi thay đổi đều sửa bản gốc trước, rồi mới cập nhật secret.

## File CSV cấu hình kênh

Các cột: `platform,channel,page_id,access_token`

- `platform`: viết thường, ví dụ `facebook`, `instagram`, `zalo`, `shopee`, `tiktok`, `tiktokshop`.
- `channel`: tên kênh. Tên này được ghi lên Lark và dùng để khớp dòng, nên **đặt đúng ngay từ đầu, không đổi về sau**. Đổi tên sẽ làm kênh bị tách thành 2 dòng khác nhau trên Lark.
- `access_token`: token của page trên Pancake. Để trống thì kênh vẫn có dòng trên Lark nhưng không có số liệu.

### File `pages_config_full_2.csv` (dùng cho bảng lead theo tháng)

Giống file CSV thường, có thêm các cột để khai báo page thuộc dòng nào trên bảng lead theo tháng:

- `lark_platform`, `lark_channel`: Nền tảng và Tên kênh của dòng trên Lark. Nhiều page có thể khai báo cùng một kênh Lark, khi đó lead được cộng dồn. Để trống thì lead của page được cộng vào dòng "Không xác định".
- `lark_project`: Dự án, dùng khi script tạo dòng mới. Để trống thì script lấy Dự án của kênh đó ở các tháng trước; kênh hoàn toàn mới chưa có Dự án sẽ được báo trong log để điền tay.

### Mã hoá CSV và cập nhật secret

Mở PowerShell (hoặc Terminal trong VS Code) tại thư mục chứa file CSV:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("pages_config_full.csv")) | Set-Clipboard
```

Chuỗi mã hoá đã được copy vào clipboard. Vào GitHub, bấm vào secret `PAGES_CONFIG_CSV_B64` → **Update** → dán vào → lưu.

Với file `pages_config_full_2.csv`: đổi tên file trong lệnh trên, rồi cập nhật secret `PAGES_CONFIG_CSV_2_B64`.

Không dùng các trang mã hoá base64 trên mạng.

## Thay đổi thường gặp

### Thêm kênh mới

1. Lấy `page_id` và `access_token` của kênh trên Pancake.
2. Thêm 1 dòng vào CSV gốc.
3. Mã hoá lại và cập nhật secret `PAGES_CONFIG_CSV_B64`.
4. Từ lần chạy sau, bảng Chat tự tạo dòng cho kênh mới và script Realtime tự quét kênh mới.
5. Nếu kênh cần có trong bảng Lead Ecom: thêm 1 dòng vào `DAILY_ROWS` trong `Extract_Lead_Ecom.py`, rồi commit.
6. Nếu kênh cần có trong bảng lead theo tháng: thêm dòng vào `pages_config_full_2.csv` gốc (điền `lark_platform`, `lark_channel`, `lark_project`), mã hoá lại và cập nhật secret `PAGES_CONFIG_CSV_2_B64`. Dòng của kênh mới được tạo tự động từ tháng hiện tại.

### Thêm sale mới

1. Thêm tài khoản sale vào các page trên Pancake.
2. Thêm 1 dòng vào bảng Lark theo dõi chia lead, cột **Tên TK POS** ghi đúng tên tài khoản Pancake (không phân biệt có dấu/không dấu, hoa/thường).
3. Nếu sale có Zalo cá nhân kết nối Pancake:
   - Thêm kênh Zalo đó vào CSV như phần thêm kênh.
   - Thêm page ID Zalo (dạng `pzl_...`) vào cuối secret `PERSONAL_ZALO_PAGE_IDS`.

   Thiếu bước thêm ID thì lead của Zalo đó bị tính vào kênh chung.

### Sale nghỉ việc

1. Xoá dòng kênh Zalo cá nhân của sale khỏi CSV, cập nhật secret.
2. Xoá page ID khỏi `PERSONAL_ZALO_PAGE_IDS`.
3. Dòng của sale trên bảng Lark: xoá hoặc giữ lại tuỳ nhu cầu lưu số liệu cũ.

### Bảng tóm tắt

| Thay đổi | CSV + `PAGES_CONFIG_CSV_B64` | `PERSONAL_ZALO_PAGE_IDS` | Bảng Lark | Code |
|---|---|---|---|---|
| Thêm kênh | Có | | | |
| Thêm kênh vào bảng Lead Ecom | Có | | | Sửa `DAILY_ROWS` |
| Thêm kênh vào bảng lead theo tháng | Có, và thêm vào `pages_config_full_2.csv` + `PAGES_CONFIG_CSV_2_B64` | | | |
| Thêm sale | | | Thêm dòng Tên TK POS | |
| Thêm Zalo cá nhân của sale | Có | Có | | |
| Sale nghỉ việc | Xoá dòng | Xoá ID | Tuỳ chọn | |

Sau mỗi thay đổi, vào tab **Actions** → chọn workflow → **Run workflow** để chạy thử ngay.

## Chạy thủ công và chạy bù

- Chạy ngay: tab **Actions** → chọn workflow → **Run workflow**.
- Chạy bù (workflow Chat và Lead Ecom): khi bấm Run workflow, nhập ô **TỪ ngày** và **ĐẾN ngày** theo dạng `dd/mm/yyyy`. Chỉ nhập "TỪ ngày" thì chạy đến hôm qua. Để trống cả hai thì chạy cho hôm qua.
- Chạy bù sẽ **ghi đè** số liệu đã có trong khoảng ngày đó bằng số mới lấy từ Pancake.
- Workflow lead theo tháng chạy bù theo **tháng**: nhập ô `backfill_from` dạng `M.YYYY` (ví dụ `1.2026`), script chạy từ tháng đó đến tháng hiện tại. Ô `dry_run = true` để chạy thử, chỉ in log, không ghi Lark. Chạy bù chỉ ghi vào dòng đã có; dòng chỉ được tự tạo cho tháng hiện tại.
- Không dùng nút **Re-run jobs** sau khi đã sửa code: nút này chạy lại đúng phiên bản code cũ.

## Đọc log và xử lý lỗi

Workflow báo đỏ khi có kênh ghi thất bại. Kênh thiếu token chỉ tính là "bỏ qua", không làm workflow đỏ.

| Thông báo | Nguyên nhân | Cách xử lý |
|---|---|---|
| `Thiếu LARK_... / PAGES_CONFIG_CSV_B64 / PERSONAL_ZALO_PAGE_IDS` | Chưa tạo secret, hoặc tên trong `env:` của file yml không khớp tên biến trong code | Kiểm tra lại tên secret và phần `env:` |
| `Extra data: line 1 column 5` hoặc `Lark trả HTTP 404` | Địa chỉ bảng Lark bị rỗng hoặc sai | Kiểm tra secret `LARK_BASE_APP_TOKEN` |
| `code: 91402, NOTEXIST` | Base hoặc bảng không tồn tại với mã đã cấu hình | Kiểm tra base token, table ID, quyền của app Lark trên base |
| `Lấy Lark token thất bại` | App ID/Secret sai hoặc đã đổi | Cập nhật `LARK_APP_ID` / `LARK_APP_SECRET` |
| `401 Unauthorized` / `Pancake 4xx` ở một kênh | Token của kênh hết hạn hoặc sai | Lấy lại token trên Pancake, cập nhật CSV và secret |
| Số dòng "tạo mới" nhiều hơn dự kiến | Tên kênh trên Lark lệch với CSV nên script tạo dòng mới | Sửa tên cho khớp từng ký tự, xoá dòng trùng |
| `N nhân viên Pancake không có trong cột 'Tên TK POS'` | Sale chưa có dòng trên Lark hoặc tên lệch | Thêm/sửa dòng trên Lark |

Script Realtime mặc định **không in số liệu theo tên sale** ra log (vì repo public). Khi cần dò lỗi, thêm `LOG_THEO_SALE: '1'` vào phần `env:` của bước chạy script trong file yml, dò xong thì xoá dòng đó.

## Lưu ý vận hành

- Repo public **60 ngày không có commit mới**, GitHub sẽ tự tắt lịch chạy của 2 workflow hằng ngày (có gửi email báo trước). Khi nhận email, vào tab Actions bật lại. Workflow Realtime chạy qua cron-job.org nên không bị ảnh hưởng.
- Token GitHub dùng cho cron-job.org có hạn dùng. Đặt nhắc lịch gia hạn trước ngày hết hạn.
- Log GitHub Actions của repo public ai cũng xem được. Không thêm lệnh `print` in ra token, số điện thoại khách hay số liệu theo tên nhân viên.
- Trước khi commit, mở Source Control trong VS Code kiểm tra danh sách file: **không được có `.csv` hoặc `.env`**.
- Nếu lỡ commit token hoặc secret: xoá khỏi code chưa đủ vì lịch sử commit vẫn lưu. Phải đổi (rotate) ngay secret/token đó trên Lark hoặc Pancake.

## Chạy trên máy cá nhân

```powershell
pip install -r requirements.txt

# Khai báo biến cho phiên PowerShell hiện tại
$env:LARK_APP_ID = "..."
$env:LARK_APP_SECRET = "..."
$env:LARK_BASE_APP_TOKEN = "..."
$env:PERSONAL_ZALO_PAGE_IDS = "pzl_...,pzl_..."

# Đặt file pages_config_full.csv cùng thư mục với code, rồi chạy
python pancake_to_lark.py
```

`Follow_lead_realtime.py` còn tự đọc file `.env` cùng thư mục nếu có. File `.env` đã bị chặn trong `.gitignore`, không đưa lên repo.
