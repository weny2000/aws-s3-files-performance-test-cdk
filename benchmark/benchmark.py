#!/usr/bin/env python3
"""
S3 Files Benchmark — ECS Container Entrypoint
==============================================
4つのファイルアクセス方式を3次元（性能・運用・コスト）で評価する。

評価シナリオ（各ファイルサイズ × 10回ループ）：
  Case 1: Standard S3              → GetObject で /tmp にダウンロード → ローカル R/W
  Case 2: S3 Express One Zone      → GetObject で /tmp にダウンロード → ローカル R/W
  Case 3: EFS                      → マウントポイント直接 R/W
  Case 4: S3 Files                 → マウントポイント直接 R/W

計測指標：
  - download_ms           … Case 1/2 のダウンロード所要時間（ms）
  - read_ms / write_ms    … 実際の R/W 所要時間（ms）
  - read/write throughput … MB/s
  - P95 / P99 レイテンシ
  - error_rate
"""

import os, sys, time, json, uuid, hashlib, statistics, traceback, tempfile, logging
import boto3
from pathlib import Path
from typing import NamedTuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("bench")

# ── constants ──────────────────────────────────────────────────────────────────
FILE_SIZES_MB   = [int(x.strip()) for x in os.environ.get("FILE_SIZES_MB", "100").split(",")]
ITERATIONS      = 10
CHUNK_SIZE      = 4 * 1024 * 1024   # 4 MB read/write chunk

# ── env ────────────────────────────────────────────────────────────────────────
REGION          = os.environ["AWS_REGION"]
STANDARD_BUCKET = os.environ["STANDARD_BUCKET"]  # Case 1 & 4
EXPRESS_BUCKET  = os.environ["EXPRESS_BUCKET"]   # Case 2
EFS_MOUNT       = os.environ["EFS_MOUNT"]         # Case 3
RESULT_PARAM    = os.environ["RESULT_PARAM"]


# ── result types ───────────────────────────────────────────────────────────────
class IterResult(NamedTuple):
    iteration:       int
    download_ms:     float   # S3 download のみ; EFS/S3Files 直接 R/W は 0
    read_ms:         float
    write_ms:        float
    read_throughput: float   # MB/s
    write_throughput:float   # MB/s
    error:           str


class CaseResult(NamedTuple):
    case:            str
    label:           str
    iterations:      list
    avg_read_mbps:   float
    avg_write_mbps:  float
    avg_download_ms: float
    p95_read_ms:     float
    p99_read_ms:     float
    p95_write_ms:    float
    p99_write_ms:    float
    error_rate:      float
    total_elapsed_s: float


# ── helpers ────────────────────────────────────────────────────────────────────
def make_test_block() -> bytes:
    """1 MB のテストデータブロックを生成（大ファイルでも OOM しない）"""
    return b'\xAB\xCD' * (512 * 1024)


def write_file(path: Path, block: bytes, size_mb: int) -> None:
    """block を size_mb 回繰り返してファイルに書き込む（ストリーミング）"""
    with open(path, "wb") as f:
        for _ in range(size_mb):
            f.write(block)


def read_file_stream(path: Path) -> None:
    """ファイルを CHUNK_SIZE ずつ読み飛ばす（メモリに蓄積しない）"""
    with open(path, "rb") as f:
        while f.read(CHUNK_SIZE):
            pass


def pct(data: list, p: float) -> float:
    if not data:
        return 0.0
    sd = sorted(data)
    k  = (len(sd) - 1) * p / 100
    f  = int(k)
    c  = min(f + 1, len(sd) - 1)
    return sd[f] + (sd[c] - sd[f]) * (k - f)


def summarize(label: str, iters: list) -> CaseResult:
    ok       = [i for i in iters if not i.error]
    err_rate = (len(iters) - len(ok)) / len(iters) if iters else 1.0
    reads    = [i.read_ms  for i in ok] or [0]
    writes   = [i.write_ms for i in ok] or [0]
    dls      = [i.download_ms for i in ok] or [0]
    return CaseResult(
        case=label.split()[0], label=label, iterations=[i._asdict() for i in iters],
        avg_read_mbps   = statistics.mean([i.read_throughput  for i in ok] or [0]),
        avg_write_mbps  = statistics.mean([i.write_throughput for i in ok] or [0]),
        avg_download_ms = statistics.mean(dls),
        p95_read_ms     = pct(reads, 95),  p99_read_ms  = pct(reads, 99),
        p95_write_ms    = pct(writes, 95), p99_write_ms = pct(writes, 99),
        error_rate      = err_rate,        total_elapsed_s=0.0,
    )


def s3_upload_with_temp(s3_client, bucket: str, key: str, block: bytes, size_mb: int,
                        storage_class: str = None) -> None:
    """一時ファイルを作成し、boto3 upload_file でアップロード（multipart 自動）"""
    tmp = Path(tempfile.gettempdir()) / f"upload_{uuid.uuid4().hex}.bin"
    try:
        write_file(tmp, block, size_mb)
        extra = {}
        if storage_class:
            extra["StorageClass"] = storage_class
        s3_client.upload_file(str(tmp), bucket, key, ExtraArgs=extra)
    finally:
        tmp.unlink(missing_ok=True)


# ── Case 1: Standard S3 → /tmp ────────────────────────────────────────────────
def run_standard_s3(block: bytes, size_mb: int, s3_client) -> CaseResult:
    label = f"Case1 Standard-S3 → /tmp ({size_mb}MB)"
    log.info("=== %s ===", label)
    t0_total = time.perf_counter()

    test_key = f"bench/{size_mb}mb_test.bin"
    log.info("Uploading test file to Standard S3 …")
    s3_upload_with_temp(s3_client, STANDARD_BUCKET, test_key, block, size_mb, "STANDARD")

    iters = []
    for i in range(1, ITERATIONS + 1):
        tmp = Path(tempfile.gettempdir()) / f"bench_{uuid.uuid4().hex}.bin"
        try:
            t0 = time.perf_counter()
            s3_client.download_file(STANDARD_BUCKET, test_key, str(tmp))
            dl_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            read_file_stream(tmp)
            r_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            write_file(tmp, block, size_mb)
            w_ms = (time.perf_counter() - t0) * 1000

            r_tp = size_mb / (r_ms / 1000)
            w_tp = size_mb / (w_ms / 1000)
            log.info("[%s] iter %02d  DL:%.0fms  R:%.0fms(%.1fMB/s)  W:%.0fms(%.1fMB/s)",
                     label, i, dl_ms, r_ms, r_tp, w_ms, w_tp)
            iters.append(IterResult(i, dl_ms, r_ms, w_ms, r_tp, w_tp, ""))
        except Exception as e:
            log.error("[%s] iter %02d ERROR: %s", label, i, e)
            iters.append(IterResult(i, 0, 0, 0, 0, 0, str(e)))
        finally:
            tmp.unlink(missing_ok=True)

    r = summarize(label, iters)
    return r._replace(total_elapsed_s=time.perf_counter() - t0_total)


# ── Case 2: S3 Express One Zone → /tmp ────────────────────────────────────────
def run_express_s3(block: bytes, size_mb: int, s3_client) -> CaseResult:
    label = f"Case2 S3-Express-OneZone → /tmp ({size_mb}MB)"
    log.info("=== %s ===", label)
    t0_total = time.perf_counter()

    test_key = f"bench/{size_mb}mb_test.bin"
    log.info("Uploading test file to S3 Express One Zone …")
    s3_upload_with_temp(s3_client, EXPRESS_BUCKET, test_key, block, size_mb)

    iters = []
    for i in range(1, ITERATIONS + 1):
        tmp = Path(tempfile.gettempdir()) / f"bench_{uuid.uuid4().hex}.bin"
        try:
            t0 = time.perf_counter()
            s3_client.download_file(EXPRESS_BUCKET, test_key, str(tmp))
            dl_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            read_file_stream(tmp)
            r_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            write_file(tmp, block, size_mb)
            w_ms = (time.perf_counter() - t0) * 1000

            r_tp = size_mb / (r_ms / 1000)
            w_tp = size_mb / (w_ms / 1000)
            log.info("[%s] iter %02d  DL:%.0fms  R:%.0fms(%.1fMB/s)  W:%.0fms(%.1fMB/s)",
                     label, i, dl_ms, r_ms, r_tp, w_ms, w_tp)
            iters.append(IterResult(i, dl_ms, r_ms, w_ms, r_tp, w_tp, ""))
        except Exception as e:
            log.error("[%s] iter %02d ERROR: %s", label, i, e)
            iters.append(IterResult(i, 0, 0, 0, 0, 0, str(e)))
        finally:
            tmp.unlink(missing_ok=True)

    r = summarize(label, iters)
    return r._replace(total_elapsed_s=time.perf_counter() - t0_total)


# ── Case 3: EFS direct R/W ───────────────────────────────────────────────────
def run_efs(block: bytes, size_mb: int) -> CaseResult:
    label = f"Case3 EFS-direct ({size_mb}MB)"
    log.info("=== %s ===", label)
    t0_total = time.perf_counter()

    bench_path = Path(EFS_MOUNT) / f"bench_{size_mb}mb.bin"
    bench_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("Writing initial %dMB file to EFS …", size_mb)
    write_file(bench_path, block, size_mb)

    iters = []
    for i in range(1, ITERATIONS + 1):
        try:
            t0 = time.perf_counter()
            read_file_stream(bench_path)
            r_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            write_file(bench_path, block, size_mb)
            w_ms = (time.perf_counter() - t0) * 1000

            r_tp = size_mb / (r_ms / 1000)
            w_tp = size_mb / (w_ms / 1000)
            log.info("[%s] iter %02d  R:%.0fms(%.1fMB/s)  W:%.0fms(%.1fMB/s)",
                     label, i, r_ms, r_tp, w_ms, w_tp)
            iters.append(IterResult(i, 0, r_ms, w_ms, r_tp, w_tp, ""))
        except Exception as e:
            log.error("[%s] iter %02d ERROR: %s", label, i, e)
            iters.append(IterResult(i, 0, 0, 0, 0, 0, str(e)))

    r = summarize(label, iters)
    return r._replace(total_elapsed_s=time.perf_counter() - t0_total)


# ── Case 4: S3 direct API — streaming GetObject / multipart PutObject ─────────
def run_s3_direct(block: bytes, size_mb: int, s3_client) -> CaseResult:
    """
    Read  = GetObject → consume body in CHUNK_SIZE chunks (no local file, no full
            in-memory load).  Measures S3 API latency without the /tmp step.
    Write = upload_file via temp file (multipart under the hood).  Avoids the
            'block * size_mb' allocation that causes OOM for large file sizes.
    """
    label = f"Case4 S3-direct-API ({size_mb}MB)"
    log.info("=== %s ===", label)
    t0_total = time.perf_counter()

    test_key = f"bench/direct/{size_mb}mb_test.bin"
    log.info("Uploading initial %dMB object to S3 (Case 4) …", size_mb)
    s3_upload_with_temp(s3_client, STANDARD_BUCKET, test_key, block, size_mb)

    iters = []
    for i in range(1, ITERATIONS + 1):
        try:
            # Read: stream response body in chunks — no intermediate /tmp file
            t0 = time.perf_counter()
            resp = s3_client.get_object(Bucket=STANDARD_BUCKET, Key=test_key)
            body = resp["Body"]
            while body.read(CHUNK_SIZE):
                pass
            r_ms = (time.perf_counter() - t0) * 1000

            # Write: temp file + multipart upload — avoids large in-memory allocation
            t0 = time.perf_counter()
            s3_upload_with_temp(s3_client, STANDARD_BUCKET, test_key, block, size_mb)
            w_ms = (time.perf_counter() - t0) * 1000

            r_tp = size_mb / (r_ms / 1000)
            w_tp = size_mb / (w_ms / 1000)
            log.info("[%s] iter %02d  R:%.0fms(%.1fMB/s)  W:%.0fms(%.1fMB/s)",
                     label, i, r_ms, r_tp, w_ms, w_tp)
            iters.append(IterResult(i, 0, r_ms, w_ms, r_tp, w_tp, ""))
        except Exception as e:
            log.error("[%s] iter %02d ERROR: %s", label, i, e)
            iters.append(IterResult(i, 0, 0, 0, 0, 0, str(e)))

    r = summarize(label, iters)
    return r._replace(total_elapsed_s=time.perf_counter() - t0_total)


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("S3 Files Benchmark starting …")
    log.info("FILE_SIZES=%s MB  ITERATIONS=%d", FILE_SIZES_MB, ITERATIONS)
    log.info("STANDARD_BUCKET=%s  (Case 1 & 4)", STANDARD_BUCKET)
    log.info("EXPRESS_BUCKET=%s   (Case 2)", EXPRESS_BUCKET)
    log.info("EFS_MOUNT=%s        (Case 3)", EFS_MOUNT)

    s3 = boto3.client("s3", region_name=REGION)
    block = make_test_block()
    all_results = {}

    for size_mb in FILE_SIZES_MB:
        log.info("")
        log.info("=" * 70)
        log.info("FILE SIZE: %d MB", size_mb)
        log.info("=" * 70)

        cases = [
            ("case1", lambda s=size_mb: run_standard_s3(block, s, s3)),
            ("case2", lambda s=size_mb: run_express_s3(block, s, s3)),
            ("case3", lambda s=size_mb: run_efs(block, s)),
            ("case4", lambda s=size_mb: run_s3_direct(block, s, s3)),
        ]

        results = {}
        for key, fn in cases:
            try:
                results[key] = fn()._asdict()
            except Exception:
                log.error("%s (%dMB) failed:\n%s", key, size_mb, traceback.format_exc())
                results[key] = {"error": traceback.format_exc()}

        all_results[f"{size_mb}mb"] = results

        # ── Summary table for this size ──────────────────────────────────────────
        log.info("")
        log.info("%s\nBENCHMARK RESULTS (%dMB)\n%s", "=" * 70, size_mb, "=" * 70)
        hdr = f"{'Case':<40} {'AvgRead':>10} {'AvgWrite':>10} {'P95Read':>10} {'P95Write':>10} {'DL avg':>9}"
        log.info(hdr)
        log.info("-" * len(hdr))
        for k, v in results.items():
            if "error" in v:
                log.info("%-40s  ERROR", k)
                continue
            log.info(
                "%-40s  %8.1fMB/s  %8.1fMB/s  %8.0fms  %8.0fms  %7.0fms",
                v.get("label", k),
                v.get("avg_read_mbps", 0), v.get("avg_write_mbps", 0),
                v.get("p95_read_ms",   0), v.get("p95_write_ms",  0),
                v.get("avg_download_ms", 0),
            )

    # ── SSM へ保存（iterations 詳細は除外してサイズを抑える） ─────────────────
    ssm_payload = {}
    for size_key, results in all_results.items():
        ssm_payload[size_key] = {}
        for k, v in results.items():
            if "error" in v:
                ssm_payload[size_key][k] = v
            else:
                ssm_payload[size_key][k] = {key: val for key, val in v.items() if key != "iterations"}

    payload = json.dumps(ssm_payload, default=str, ensure_ascii=False)
    try:
        boto3.client("ssm", region_name=REGION).put_parameter(
            Name=RESULT_PARAM, Value=payload, Type="String", Overwrite=True)
        log.info("Results saved to SSM: %s", RESULT_PARAM)
    except Exception as e:
        log.error("SSM save failed: %s", e)
        log.info("RESULTS JSON:\n%s", payload)

    log.info("Benchmark complete.")


if __name__ == "__main__":
    main()
