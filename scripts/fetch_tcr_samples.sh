#!/usr/bin/env bash
# 下载 TCR 战队公开的 RM2026 赛场 IQ 测试包（Apache-2.0，约 19 MB）到 data/tcr/，校验 SHA-256 后解压。
# 校验值写死在这里，不依赖下载源自带的 .sha256。只用其中的 .c64 和 manifest.json，不运行包里的脚本。
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data/tcr"
NAME=tcr_rm2026_single_channel_iq_test_pack_v1
URL=https://github.com/coach114514/TCR-RM2026-Radar-OpenSource/releases/download/v1.0.0-rc1/$NAME.tar.gz
SHA256=a78bf35d53d4fdd7684af22322375a849ecedd9483821cce5df77b0937f80499

if [ -f "$DEST/$NAME/manifest.json" ]; then
  echo "已存在：$DEST/$NAME"
  exit 0
fi
mkdir -p "$DEST"
cd "$DEST"
curl -fsSLO "$URL"
echo "$SHA256  $NAME.tar.gz" | sha256sum -c -
tar -xzf "$NAME.tar.gz"
echo "完成：$DEST/$NAME"
