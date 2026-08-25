# RTO/RPO Evidence — Lab 23

**Sinh viên:** Nguyễn Trường An

**MSSV:** 2A202601151

**Drill:** Region A outage (`netblock --mock`), failover sang Region B

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-08-25T04:51:17Z | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.5s | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | Không có | 16 request sau outage đều lỗi | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage (mốc 0) | 0.0s | `action:kill`, Region A | `chaos/chaos-events.jsonl:7` |
| User thấy lỗi đầu tiên | 0.5s | dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | 15.0s | `to:UNHEALTHY`, Region A, 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 12.2s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:6` |
| Region phụ ready | 18.2s | `step:4_wait_ready` | `reports/failover-events.jsonl:8` |
| DNS cutover | 18.2s | `step:5_dns_cutover` | `reports/failover-events.jsonl:9` |
| **RTO đo được** | **22.5s** | request `ok:true` đầu tiên từ Region B sau lỗi | `reports/drill-2-withdr.jsonl:37` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | **22.5s** | 300s | **PASS** |
| RPO — Vector DB | **28.0s / 14 document** | 300s | **PASS** |

Kết quả máy đo đầy đủ (`valid:true`, `warnings:[]`, `recovered_by_region:b`) nằm tại `reports/measure-drill-2.json`.

## 3. RTO breakdown trên critical path

| Thành phần | Giây | Evidence / cách tính | Giảm bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | 15.0s | `interval_s=5 × threshold=3` tại `reports/health-events.jsonl:2` | Giảm interval nhưng phải giám sát false positive và tải probe |
| Snapshot restore trên critical path | 0.0s | Restore hoàn tất trước khi health checker phát cảnh báo; thao tác thực tế <0.01s tại `reports/failover-events.jsonl:6`–`7` | Snapshot nhỏ hơn, restore song song, kiểm tra định kỳ |
| GPU pool warm-up còn lại sau detection | 3.2s | Ready/cutover +18.2s trừ detect +15.0s; tổng warm-up thực tế 6.03s và một phần chạy chồng lúc xác nhận outage tại `reports/failover-events.jsonl:8` | Giữ warm pool hoặc pre-warm có kiểm soát |
| DNS/LB TTL cache | 4.3s | RTO 22.5s trừ cutover 18.2s | TTL thấp hơn hoặc global LB health routing |
| **Tổng critical path** | **22.5s** | 15.0 + 0.0 + 3.2 + 4.3 | Đúng bằng RTO đo từ loadgen |

Việc restore và warm-up bắt đầu từ runbook trong lúc health checker độc lập đang tích lũy đủ ba lần fail, nên các khoảng thời gian có chồng lấp. Bảng dùng phần thời gian nằm trên critical path để tránh cộng trùng.
