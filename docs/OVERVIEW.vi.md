# Ứng dụng Cộng tác Client–Server Phân tán dựa trên Streaming

**Đồ án Ứng dụng Mạng**

| Mục | Chi tiết |
| --- | --- |
| Môn học | Phát triển Ứng dụng Mạng |
| Thời lượng | 6 tuần (đồ án làm tại nhà) |
| Hình thức | Cá nhân / Nhóm đôi |

> Bản tiếng Việt của tài liệu mô tả đồ án trong [`docs/OVERVIEW.md`](docs/OVERVIEW.md).

## I. Giới thiệu

### 1. Thực trạng

Khi làm việc từ xa, nhóm phân tán và hệ thống phi tập trung trở nên phổ biến, nhu cầu về ứng dụng mạng thời gian thực, ổn định cũng tăng theo. Các tổ chức cần trao đổi dữ liệu thông suốt trên mạng cục bộ lẫn mạng diện rộng, đồng thời xử lý độ trễ, mất gói tin và bảo mật dữ liệu. Biết cách xây dựng phần mềm mạng có khả năng chịu lỗi từ nền tảng là kỹ năng quan trọng của kỹ sư phần mềm và kỹ sư mạng hiện nay.

### 2. Nhu cầu của ứng dụng

Các giải pháp thương mại (Slack, Microsoft Teams, lưu trữ đám mây) thường chuyển dữ liệu qua máy chủ tập trung của bên thứ ba. Điều này có thể gây lo ngại về quyền riêng tư, tuân thủ và băng thông trong môi trường mạng khép kín. Cần một ứng dụng mạng nhẹ, bảo mật và tự lưu trữ, để người dùng trên cùng mạng (hoặc VPN) giao tiếp và chia sẻ dữ liệu trực tiếp mà không phụ thuộc hạ tầng của bên ngoài.

### 3. Mục tiêu

Mục tiêu của đồ án là xây dựng một ứng dụng cộng tác client–server phân tán, dựa trên streaming. Sinh viên thiết kế và hiện thực giao thức tầng ứng dụng riêng trên **UDP streaming**, hỗ trợ nhắn tin thời gian thực, khám phá người dùng đang hoạt động và truyền tệp tin cậy giữa nhiều client trên mạng. Vì đây là ứng dụng nhắn tin, truyền thông phải đáng tin cậy.

### 4. Tính năng

- **Xác thực và khám phá người dùng:** Đăng nhập, phát hiện hiện diện và xem danh sách người dùng đang hoạt động trên mạng.
- **Nhắn tin dựa trên streaming:** Hỗ trợ broadcast (chat chung) và unicast (tin nhắn riêng). Tham khảo Steam GameNetworkingSockets và Google QUIC cho tầng vận chuyển hướng thông điệp.
- **Truyền tệp tin cậy:** Gửi và nhận tệp nhiều định dạng, nhiều kích thước mà không làm hỏng dữ liệu.
- **Khả năng chịu lỗi kết nối:** Ứng dụng xử lý ngắt kết nối đột ngột, timeout và gián đoạn mạng mà không sập.
- **Truyền dữ liệu an toàn:** Mã hóa payload cơ bản để giảm nguy cơ bị nghe lén gói tin.

### 5. Người dùng mục tiêu

- Nhóm nội bộ trong doanh nghiệp, vận hành trên mạng LAN an toàn, cách ly hoặc air-gapped.
- Sinh viên hoặc nhóm học tập cần cộng tác và chia sẻ tệp nhanh theo mô hình ngang hàng khi không có Internet.
- Người dùng coi trọng quyền riêng tư và muốn công cụ giao tiếp tự lưu trữ.

### 6. Kỹ thuật và công nghệ

- **Lập trình socket:** TCP cho truyền tệp/tin nhắn tin cậy và UDP cho heartbeat cùng broadcast khám phá.
- **Đồng thời:** Đa luồng hoặc I/O bất đồng bộ để phục vụ nhiều kết nối cùng lúc mà không chặn luồng chính.
- **Thiết kế giao thức tầng ứng dụng:** Cấu trúc gói tin riêng (header, payload, cờ kết thúc thông điệp).
- **Đánh số thứ tự và sắp xếp:** Số thứ tự đơn điệu cùng cửa sổ trượt để điều khiển luồng và giao theo đúng thứ tự.
- **Xác nhận và truyền lại:** Chỉ truyền lại các đoạn bị thiếu.
- **Điều khiển tắc nghẽn:** Hỗ trợ streaming với BBR / CUBIC.
- **Mật mã:** Socket TLS/SSL hoặc mã hóa đối xứng (ví dụ AES) cho payload.
- **Tuần tự hóa:** Mã hóa và giải mã dữ liệu có cấu trúc (JSON, XML hoặc đóng gói nhị phân riêng) khi truyền trên mạng.

### 7. Yêu cầu thiết kế

- **Kiến trúc:** Tách rõ logic mạng, xử lý dữ liệu và giao diện người dùng.
- **Streaming nhạy với thời gian:** Hệ thống phải có cơ chế bất đồng bộ để lấy dữ liệu từ stream. Streaming chỉ xử lý các khối byte trên UDP; logic giao thức nằm ở tầng ứng dụng, nhằm loại bỏ hiện tượng chặn đầu dòng (Head-of-line blocking).
- **Giao diện:** Giao diện dòng lệnh (CLI) hoặc giao diện đồ họa (GUI), dễ dùng và hiển thị rõ trạng thái mạng.
- **Khả năng mở rộng:** Server/tracker phải phục vụ hiệu quả ít nhất 10–20 client đồng thời.

### 8. Yêu cầu kỹ thuật

- **Ngôn ngữ:** Python, Java, C++ hoặc Go (giảng viên xác nhận nếu có ngôn ngữ bị hạn chế).
- **Không dùng thư viện mạng mức cao:** Phải dùng socket thô (`socket` trong Python, `java.net.Socket` trong Java, v.v.). Cấm dùng framework HTTP (Flask/Django) hoặc WebSocket để bảo đảm sinh viên thực hành khái niệm mạng cốt lõi.
- **Môi trường:** Chạy được trên hệ điều hành thông dụng (Windows, macOS hoặc Linux) qua localhost hoặc địa chỉ IP cục bộ.

### 9. Quy trình phát triển và thang điểm (tổng 10 điểm)

Đồ án gồm 10 bước. Mỗi bước hoàn thành được **1 điểm**.

| Bước | Mốc | Điểm |
| --- | --- | --- |
| 1 | **Thiết lập và thiết kế giao thức:** Xác định cấu trúc gói tin, loại thông điệp (ví dụ AUTH, MSG, FILE) và sơ đồ trạng thái trong tài liệu trước khi viết mã. | 1 điểm |
| 2 | **Bắt tay client–server cơ bản:** Kết nối socket TCP để client kết nối tới server và nhận xác nhận. | 1 điểm |
| 3 | **Xác thực và quản lý trạng thái:** Cơ chế đăng nhập. Server theo dõi người dùng đã kết nối, địa chỉ IP và cổng đang dùng. | 1 điểm |
| 4 | **Đồng thời:** Đa luồng hoặc I/O bất đồng bộ để server xử lý nhiều client kết nối và ngắt kết nối cùng lúc. | 1 điểm |
| 5 | **Streaming nhạy với thời gian:** Định tuyến để client gửi tin tới server, server phát hoặc chuyển tiếp đúng người nhận. | 1 điểm |
| 6 | **Truyền tệp tin cậy:** Tuần tự hóa tệp, chia khối (với tệp lớn) và ghép lại trên UDP mà không làm hỏng dữ liệu. | 1 điểm |
| 7 | **Chịu lỗi mạng và xử lý lỗi:** Bắt lỗi timeout socket, đường ống bị đứt và thoát client có kiểm soát, server không bị sập. | 1 điểm |
| 8 | **Lớp bảo mật:** Mã hóa payload (bằng thư viện mật mã chuẩn) trước khi gửi và giải mã khi nhận. | 1 điểm |
| 9 | **Giao diện HTTP chỉ với JavaScript phía client:** Giao diện hiển thị rõ tin nhắn đến, thanh tiến trình truyền tệp và danh sách người dùng đang hoạt động. | 1 điểm |
| 10 | **Kiểm thử và chất lượng mã:** Mã được định dạng, có chú thích và vượt qua mô phỏng mạng cục bộ (kết nối từ nhiều cửa sổ terminal). | 1 điểm |
| **Tổng** | | **10 điểm** |

### 10. Hướng dẫn nộp bài

**Hạn nộp gợi ý:** Chủ Nhật, 17 tháng 11 năm 2026, 23:59 (theo đề bài).

**Nội dung cần nộp:**

Nộp tệp `.zip` hoặc đường dẫn kho GitHub riêng tư, gồm:

1. **Mã nguồn:**
   - Toàn bộ script client và server.
   - Tệp `README.md` với hướng dẫn từng bước: cài phụ thuộc, chạy server và kết nối client.
2. **Báo cáo kỹ thuật (PDF):**
   - **Kiến trúc hệ thống:** Sơ đồ mức cao về cách client và server tương tác.
   - **Đặc tả giao thức:** Cấu trúc gói tin/thông điệp đã thiết kế.
   - **Sơ đồ tuần tự:** Luồng vòng đời của một tin nhắn và một lần truyền tệp.
   - **Khó khăn và cách giải:** Phản ánh khoảng 1 trang về các vấn đề mạng gặp phải (ví dụ deadlock, phân mảnh gói tin) và cách xử lý.

---

> **Ghi chú:** Tài liệu này là bản Markdown của đề bài, không phải tài liệu hiện thực. Đề gốc đề cập cả TCP và UDP ở các yêu cầu khác nhau; nội dung được giữ nguyên như đã viết. Hướng dẫn cài đặt và chạy riêng của dự án sẽ được bổ sung khi đã có chi tiết hiện thực.
