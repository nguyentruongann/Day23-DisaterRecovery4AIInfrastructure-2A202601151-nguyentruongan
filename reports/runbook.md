# Runbook — Region chính down

**Owner tài liệu:** SRE/Platform · **Hệ thống:** AI Inference API · **RTO:** 300s · **RPO:** 300s

| # | Bước | Lệnh copy-paste | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A không ready trong 3 lần kiểm tra liên tiếp; Region B vẫn `alive:true` | On-call SRE |
| 2 | Mở incident và bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` | Log `thong_bao_incident` xuất hiện trong `reports/runbook-run.jsonl` và operator xác nhận `y` | Incident Commander |
| 3 | Restore state ở Region B | `python3 state/snapshot.py get --region b --backend fs` | Lệnh trả `snapshot_at`, `restored_at`, `embed_model_version`; `failover-events.jsonl` có `2_restore_snapshot` | Data/ML Platform |
| 4 | Scale pool warm → full | `printf 'full\n' > state/region-b/pool_state && until curl -sf http://127.0.0.1:8002/readyz; do sleep 1; done` | `/readyz` của B trả HTTP 200, `ready:true`, weights có và vector count > 0 | ML Serving On-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region && curl -s http://127.0.0.1:8080/edge/state` | Sau tối đa một TTL, `active_region` là `b`; automation có event `5_dns_cutover` | Network/SRE |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -sf http://127.0.0.1:8002/v1/infer >/dev/null || echo FAIL-$i; done` | 10/10 request thành công; runbook ghi error rate 0 và p95 < 500ms | On-call + Product |
| 7 | Đo RTO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:PASS`, recovery do Region B phục vụ | Incident Commander |

## Điều kiện dừng và rollback

- **Dừng cutover:** nếu restore lỗi, sai `embed_model_version`, vector count bằng 0, Region B không `ready:true` trong 60s, hoặc Region B mất liveness. Không được ghi `edge/active_region=b`.
- **Rollback về Region A:** chỉ khi Region A đã `ready:true` ổn định tối thiểu 3 probe liên tiếp, state đã reconcile và golden signals đạt error rate <1%, p95 <500ms trong 10 request.
- **Thẩm quyền:** Incident Commander quyết định rollback sau khi SRE và Data/ML Platform cùng xác nhận. Không tự động failback để tránh flapping hai chiều.
- **Lệnh rollback:** `printf a > edge/active_region && curl -s http://127.0.0.1:8080/edge/state`.
