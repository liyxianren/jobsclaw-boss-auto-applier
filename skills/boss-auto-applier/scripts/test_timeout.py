#!/usr/bin/env python3
"""测试 OpenClaw exec 超时机制 v2。

Test A (默认): 每 10 秒输出一次心跳，总计跑 5 分钟。
  → 如果输出能重置 no-output timer，应该能跑完。
  → 如果 60s 是 overall timeout，跑到 60s 就会被杀。

Test B (--silent): 完全不输出，看多久被杀。
  → 确认 no-output timeout 精确值。

用法:
  python3 test_timeout.py          # Test A: 带心跳
  python3 test_timeout.py --silent  # Test B: 纯静默
"""

import signal
import sys
import time

start = time.time()
silent = "--silent" in sys.argv

def on_sigterm(signum, frame):
    elapsed = time.time() - start
    print(f"[SIGTERM] Received at {elapsed:.1f}s (mode={'silent' if silent else 'heartbeat'})", flush=True)
    sys.exit(143)

signal.signal(signal.SIGTERM, on_sigterm)

mode = "SILENT" if silent else "HEARTBEAT (10s interval)"
print(f"[START] test_timeout.py v2 — mode: {mode}", flush=True)

if silent:
    # Test B: 完全静默，看多久被杀
    time.sleep(600)
else:
    # Test A: 每 10 秒输出心跳，持续 5 分钟
    for tick in range(1, 31):  # 30 ticks × 10s = 300s = 5 min
        time.sleep(10)
        elapsed = time.time() - start
        print(f"[HEARTBEAT {tick}] alive at {elapsed:.1f}s", flush=True)

elapsed = time.time() - start
print(f"[DONE] Completed in {elapsed:.1f}s", flush=True)
