# Breathing

A macOS menu bar app that reminds you to close your mouth when you're mouth-breathing.

It watches your webcam (1 frame per second), measures how far apart your lips are using
Google's MediaPipe face landmarker, and sends a "Close your mouth" notification when your
mouth has stayed open for about 8 seconds. Talking doesn't trigger it. Everything runs
locally — no video leaves your Mac.

## Setup

```bash
./setup.sh
```

## Run

```bash
./run.sh
```

Run it from the Terminal app the first time and allow camera access when macOS asks.
A 🤐 icon appears in the menu bar. On first launch it calibrates for a few seconds —
look at the screen with your mouth closed.

## Menu

- **Status line** — live mouth reading and today's reminder count
- **Pause / Resume** — releases the camera entirely (webcam light turns off)
- **Recalibrate** — re-learn your closed-mouth baseline
- **Send test notification** — check that notifications get through
- **Camera** — choose which camera to use (defaults to the built-in one)

If notifications don't appear, allow them for **Script Editor** in
System Settings → Notifications.

## Tuning

Constants at the top of `breathing.py`:

| Setting | Default | Meaning |
|---|---|---|
| `FPS` | 1 | Frames analysed per second |
| `WINDOW_SECONDS` | 8 | How long the mouth must look open before a reminder |
| `OPEN_FRACTION` | 0.7 | Share of frames in that window that must be open |
| `OPEN_MARGIN` | 0.015 | Lip gap above your resting baseline that counts as open |
| `COOLDOWN_SECONDS` | 90 | Minimum time between reminders |

Calibration and camera choice are saved in `~/.breathing.json`.

## Limitations

- The webcam light stays on while monitoring; macOS wires it to the camera in hardware.
- It detects open lips, not airflow, so breathing through slightly parted lips may be missed.
