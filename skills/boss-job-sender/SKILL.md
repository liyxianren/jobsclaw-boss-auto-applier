---
name: boss-job-sender
description: "BOSS直聘 Stage 3。负责读取 eval_summary，批量发送消息并输出最终 receipt。"
user-invocable: false
allowed-tools: Bash(python3:*), Bash(curl:*)
---

# BOSS 批量发送（Stage 3）

> 本阶段只做发送、重试分类、对账和回执，不做 fit 判断，不生成文案。

## 输入
- `RUN_DIR`
- `EVAL_SUMMARY_PATH`
- `CDP_PORT`
- `MAX_SEND`
- `DRY_RUN`

## 执行入口
```bash
python3 skills/boss-auto-applier/scripts/send_batch.py \
  --eval-summary ${EVAL_SUMMARY_PATH} \
  --run-dir ${RUN_DIR} \
  --cdp-port ${CDP_PORT} \
  --max-send ${MAX_SEND} \
  $([ "${DRY_RUN}" = "true" ] && echo "--dry-run")
```

## 实际链路
- `send_batch.py`
- `sender_worker.py`
- `send_greeting.py`
- `reconcile_receipt.py`

## 输出
- `${RUN_DIR}/send/<jobId>.json`
- `${RUN_DIR}/logs/stage3_send.jsonl`
- `${RUN_DIR}/receipt.json`

## 发送规则
- 只读取 `eval_summary.json.fitJobs`
- 优先读取每个岗位对应 `eval/<jobId>.json` 中的 `message` / `messageDraft`
- 同一 run 已尝试过的 jobId 不重复发送
- 最终统计以 `receipt.json` 和对账结果为准

## 参考
- `../jd-greeting-generator/references/boss-chat-elements.md`
