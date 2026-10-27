# tv-live-subs

Live translated subtitles over any Android TV app (target: Xiaomi Mi Box, Android 11).
Audio is captured on-device (MediaProjection + AudioPlaybackCapture), sent to a cloud
STT/translation service and rendered in an opaque bottom overlay.

## Status

Stage 3: audio level meter prototype (capture + dBFS bar in the overlay) to find out which
TV apps allow playback capture.

## Build & install

`local.properties` (git-ignored) needs `sdk.dir=...`. Another device/emulator may be attached,
so always pass `-s`:

```sh
export DEV=192.168.1.49:5555
./gradlew :app:assembleDebug
adb -s $DEV install -r app/build/outputs/apk/debug/app-debug.apk
adb -s $DEV shell appops set dev.tvlivesubs SYSTEM_ALERT_WINDOW allow
adb -s $DEV shell pm grant dev.tvlivesubs android.permission.RECORD_AUDIO
adb -s $DEV logcat -s LevelMeter
```

Launching the app toggles capture (start → MediaProjection consent dialog; run again → stop).

## Server (Mac, `server/`)

Streaming STT + translation over WebSocket. Needs `brew install whisper-cpp ffmpeg uv`.

```sh
cd server
cp .env.example .env            # optional: GEMINI_API_KEY for --translator gemini
uv sync --extra local           # mlx-lm for the local translator (Apple Silicon)
uv run tvsubs-server --preload auto he
```

- **STT:** whisper.cpp (`whisper-server`, Metal). Hebrew uses the ivrit-ai fine-tune; other languages the stock
  large-v3-turbo. The source language is detected per segment by a small stock model (`--languages` limits it).
- **Translation (`--translator auto`):** local Gemma 4 E4B via mlx-lm if downloaded, else Gemini if a key is set,
  else NLLB. `--local-model` picks a bigger one (12B 9 GB, 26B-A4B 22 GB: better by ~0.05-0.1 on a blind test,
  but they don't keep up with live speech next to Whisper on a 34 GB Mac).
- **Glossary:** `glossary.txt` (`[lang]` sections, `source = target`). Terms are also learned from the stream and kept
  in `learned_terms.tsv` (git-ignored); they reach the prompts after being seen twice. Edit or promote them by hand.
- `--record DIR` saves each session's audio for offline replay: `uv run python test_client.py rec.wav --src auto --dst ru`.
