#!/usr/bin/env bash
# 禁止再装已知坏版 nt_helper；校验生产 native 与 pin 一致。
# 坏版：nt-helper-20261105-02c56a4 → PbSendMsg 假成功（groupSequence=0）。
set -euo pipefail

NATIVE=${NATIVE:-/opt/whale/weq/weq-web/native/linux/x64/nt_helper.node}
PIN=${PIN:-/opt/whale/weq/weq-src/native/pinned.json}
NODE=${NODE:-/opt/node-v24.21.0/bin/node}
# md5 of the known-bad binary archived at rollback
BAD_MD5S=${BAD_MD5S:-"faf67f78aa2a07661b46a87e39801349"}
# allowed pin tags (good)
ALLOWED_TAGS=${ALLOWED_TAGS:-"nt-helper-20261029-550ce33"}
BLOCKED_TAGS=${BLOCKED_TAGS:-"nt-helper-20261105-02c56a4"}

LOGDIR=${LOGDIR:-/opt/whale/state}
LOG=$LOGDIR/nt-helper-pin.log
mkdir -p "$LOGDIR"

ts() { date '+%F %T'; }

if [[ ! -f $NATIVE ]]; then
  echo "$(ts) MISSING $NATIVE" | tee -a "$LOG" >&2
  exit 1
fi

md5=$(md5sum "$NATIVE" | awk '{print $1}')
for bad in $BAD_MD5S; do
  if [[ $md5 == "$bad" ]]; then
    echo "$(ts) BLOCKED bad nt_helper md5=$md5（假成功版，禁止运行）" | tee -a "$LOG" >&2
    exit 2
  fi
done

if [[ -f $PIN ]]; then
  tag=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("tag",""))' "$PIN")
  for blocked in $BLOCKED_TAGS; do
    if [[ $tag == "$blocked" ]]; then
      echo "$(ts) BLOCKED pinned tag=$tag（已知坏版）" | tee -a "$LOG" >&2
      exit 2
    fi
  done
  ok=0
  for allowed in $ALLOWED_TAGS; do
    [[ $tag == "$allowed" ]] && ok=1
  done
  if [[ $ok -ne 1 ]]; then
    echo "$(ts) WARN pin tag=$tag 不在 ALLOWED_TAGS=[$ALLOWED_TAGS]；请人工确认后再升" | tee -a "$LOG"
  else
    echo "$(ts) OK pin tag=$tag md5=$md5" | tee -a "$LOG"
  fi
else
  echo "$(ts) WARN no pinned.json；仅校验坏版 md5 黑名单（md5=$md5）" | tee -a "$LOG"
fi

# 仍跑有效期检查（若脚本存在）
if [[ -x /opt/whale/scripts/check-nt-helper-expiry.sh ]]; then
  /opt/whale/scripts/check-nt-helper-expiry.sh || true
fi
exit 0
