# Postmortem — DR Drill Lab 23

**Sinh viên:** Nguyễn Trường An · **MSSV:** 2A202601151

**Phạm vi:** Region A netblock, chuyển traffic sang Region B · **Tính chất:** blameless drill

## 1. Timeline

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T04:59:42Z | Outage Region A bắt đầu | `chaos/chaos-events.jsonl:7` |
| 2026-08-25T04:59:43Z | User đầu tiên bị ảnh hưởng (+0.5s) | `reports/drill-2-withdr.jsonl:26` |
| 2026-08-25T04:59:54Z | Operator xác nhận outage sau 3 probe và mở incident (+12.2s) | `reports/runbook-run.jsonl:1`–`2` |
| 2026-08-25T04:59:57Z | Health checker đánh dấu Region A UNHEALTHY (+15.0s) | `reports/health-events.jsonl:2` |
| 2026-08-25T05:00:00Z | Region B ready và DNS cutover (+18.2s) | `reports/failover-events.jsonl:8`–`9` |
| 2026-08-25T05:00:05Z | Request đầu tiên thành công từ Region B; incident resolved (+22.5s) | `reports/drill-2-withdr.jsonl:37` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: 300s; đo được: **22.5s**; còn dư **277.5s** so với ngân sách.
- RPO mục tiêu: 300s; đo được: **28.0s / 14 document mất**; còn dư **272.0s** theo thời gian.
- Bước lớn nhất là **health-check detection floor 15.0s**, chiếm **66.7% RTO**. Nguyên nhân là anti-flap yêu cầu 3 probe liên tiếp với interval 5s.
- Restore kết thúc trước alert chính thức và GPU warm-up chạy chồng với thời gian phát hiện; phần warm-up còn lại trên critical path là 3.2s. DNS TTL thêm 4.3s.
- Golden signals sau cutover: 10/10 request thành công, error rate 0%, p95 22.09ms tại `reports/runbook-run.jsonl:6`.

## 3. Root cause — 5 whys

1. Tại sao user lỗi? Edge vẫn cache Region A sau khi Region A ngừng phản hồi.
2. Tại sao traffic chưa chuyển ngay? Hệ thống cần chống flapping bằng ba lần readiness probe thất bại liên tiếp.
3. Tại sao không thể chuyển thẳng sang B? B ban đầu chỉ alive nhưng chưa có weights, vectors và pool vẫn warm.
4. Tại sao cần restore trong incident? Kiến trúc active-passive chỉ replicate snapshot định kỳ 30s, không duy trì B ở trạng thái serving-ready.
5. Tại sao đây còn là rủi ro trong outage thật? Health checker và runbook chưa có một bộ điều phối có circuit breaker/policy approval; nếu operator không phản hồi, RTO có thể vượt mục tiêu dù các script riêng lẻ đúng.

Root cause hệ thống là **Region B không serve-ready liên tục và quy trình cutover còn phụ thuộc xác nhận vận hành**, không phải người chạy chaos script.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thêm controller nhận event UNHEALTHY, mở incident và cung cấp one-click failover có circuit breaker | SRE Platform | 2026-09-08 | Giảm 5–10s thao tác; không hy sinh anti-flap |
| 2 | Giữ Region B ở warm standby có weights + snapshot được verify mỗi 30s; cảnh báo replication lag >60s | ML/Data Platform | 2026-09-15 | Loại phần lớn 6.03s warm-up và giữ RPO có kiểm soát |
| 3 | Thử TTL 2s trong game day và theo dõi tải edge | Network SRE | 2026-09-15 | Giảm tối đa khoảng 3s DNS cache |

## 5. Ba câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`, chiếm **15 / 22.5 = 66.7% RTO**. Với mục tiêu 5 phút và threshold 3, trần lý thuyết của interval là 100s, nhưng thực tế phải chừa ngân sách restore/warm-up/TTL nên cần thấp hơn nhiều.
2. Nếu interval giảm còn 1s thì detect floor còn 3s, giảm tối đa 12s. Đổi lại là tải probe tăng 5 lần, nhạy hơn với jitter và có nguy cơ false positive/flapping; cần circuit breaker và quan sát tỷ lệ lỗi trước khi áp dụng.
3. Nếu outage kéo dài 6 giờ và primary mất vĩnh viễn, `docs_lost=14` nghĩa là 14 document được ghi sau snapshot gần nhất không có trong bản restore tại thời điểm failover. Khách hàng có thể mất ticket/yêu cầu gần nhất cho đến khi có nguồn khác để reconcile; đây là dữ liệu mất thực tế, không chỉ là một con số thời gian.
