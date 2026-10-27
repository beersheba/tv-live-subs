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
