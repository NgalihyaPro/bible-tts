#!/usr/bin/env bash
# Regenerate the Swahili Bible with the corrected pipeline.
# Up to 3 passes: each pass skips completed chapters and retries any that
# failed validation (the generator deletes those, so they reappear as missing).
export PATH="/c/Users/alfre/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0-full_build/bin:$PATH"
cd /e/webguru/piper-tts
for pass in 1 2 3; do
  echo "########## PASS $pass ##########"
  ./.venv/Scripts/python.exe scripts/generate_chapters.py \
    --voice voices/sw/sw_CD-lanfrica-medium.onnx \
    --bible data/bible/sw/suv.json \
    --language sw \
    --out audio_v2 \
    --workers 4
  done_count=$(find audio_v2 -name '*.m4a' | wc -l)
  echo "########## after pass $pass: $done_count / 1189 ##########"
  [ "$done_count" -ge 1189 ] && break
done
echo "########## GENERATION LOOP COMPLETE ##########"
