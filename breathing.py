"""Menu bar app that reminds you to close your mouth when you mouth-breathe.

Watches the webcam with MediaPipe's face landmarker, measures the gap between
your inner lips (relative to face size), and shows a "Close your mouth" banner
when your mouth has stayed open for a while. The banner disappears once you
close your mouth. Everything runs locally.
"""

import collections
import json
import os
import statistics
import threading
import time

import cv2
import mediapipe as mp
import rumps
from AppKit import (
    NSBackingStoreBuffered,
    NSBox,
    NSBoxCustom,
    NSColor,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSSound,
    NSStatusWindowLevel,
    NSTextAlignmentCenter,
    NSTextField,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python import vision

from camera import Camera, ensure_access, list_cameras

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "models", "face_landmarker.task")
CALIBRATION_PATH = os.path.expanduser("~/.breathing.json")

# --- Tuning -----------------------------------------------------------------
FPS = 3                    # frames analysed per second (low = light on CPU and battery)
WINDOW_SECONDS = 2         # how long the mouth must look open before a reminder
OPEN_FRACTION = 0.8        # share of frames in the window that must be "open"
                           # (talking closes the mouth often, so it won't trigger)
OPEN_MARGIN = 0.015        # lip gap above your resting baseline that counts as open
COOLDOWN_SECONDS = 10      # minimum time between reminders
CALIBRATION_SECONDS = 4    # mouth-closed sampling time for calibration
FACE_LOST_SECONDS = 3      # only reset the timer if your face is gone this long
ANALYSIS_WIDTH = 640       # frames are downscaled to this width before face tracking
SOUND_NAME = "Purr"        # macOS system sound played with the banner ("" for silent)
SOUND_VOLUME = 0.35        # 0.0–1.0

# Face mesh landmark indices
UPPER_INNER_LIP = 13
LOWER_INNER_LIP = 14
FOREHEAD = 10
CHIN = 152


class ReminderBanner:
    """Floating "Close your mouth" banner in the top-right corner. Main thread only."""

    WIDTH, HEIGHT, MARGIN = 240, 52, 12

    def __init__(self):
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSStatusWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setIgnoresMouseEvents_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )

        view = NSBox.alloc().initWithFrame_(NSMakeRect(0, 0, self.WIDTH, self.HEIGHT))
        view.setBoxType_(NSBoxCustom)
        view.setBorderWidth_(0)
        view.setCornerRadius_(14)
        view.setFillColor_(NSColor.colorWithCalibratedWhite_alpha_(0.12, 0.92))
        view.setTitlePosition_(0)  # NSNoTitle
        view.setContentViewMargins_((0, 0))
        panel.setContentView_(view)

        label = NSTextField.labelWithString_("🤐  Close your mouth")
        label.setFont_(NSFont.boldSystemFontOfSize_(17))
        label.setTextColor_(NSColor.whiteColor())
        label.setAlignment_(NSTextAlignmentCenter)
        label.sizeToFit()
        size = label.frame().size
        label.setFrameOrigin_(((self.WIDTH - size.width) / 2, (self.HEIGHT - size.height) / 2))
        view.contentView().addSubview_(label)

        self.panel = panel
        self.visible = False

    def show(self):
        if self.visible:
            return
        screen = NSScreen.mainScreen().visibleFrame()
        x = screen.origin.x + screen.size.width - self.WIDTH - self.MARGIN
        y = screen.origin.y + screen.size.height - self.HEIGHT - self.MARGIN
        self.panel.setFrameOrigin_((x, y))
        self.panel.orderFrontRegardless()
        sound = NSSound.soundNamed_(SOUND_NAME) if SOUND_NAME else None
        if sound is not None:
            sound.setVolume_(SOUND_VOLUME)
            sound.play()
        self.visible = True

    def hide(self):
        if self.visible:
            self.panel.orderOut_(None)
            self.visible = False


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def lip_gap_ratio(landmarks):
    """Inner-lip gap divided by face height, so distance from the camera doesn't matter."""
    def dist(a, b):
        pa, pb = landmarks[a], landmarks[b]
        return ((pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2) ** 0.5

    face_height = dist(FOREHEAD, CHIN)
    if face_height <= 0:
        return None
    return dist(UPPER_INNER_LIP, LOWER_INNER_LIP) / face_height


class MouthDetector:
    """Decides, frame by frame, whether to fire a reminder. No camera code here."""

    def __init__(self, baseline=None, now=time.monotonic):
        self.baseline = baseline
        self.now = now
        self.window = collections.deque()  # (timestamp, is_open)
        self.last_alert = -1e9
        self.last_face = -1e9
        self._calib_samples = []
        self._calib_started = None

    @property
    def calibrating(self):
        return self._calib_started is not None

    def start_calibration(self):
        self._calib_samples = []
        self._calib_started = self.now()
        self.window.clear()

    def mouth_closed(self, ratio):
        """True once the mouth is closed again, or the face has been gone a while."""
        if ratio is None:
            return self.now() - self.last_face > FACE_LOST_SECONDS
        return self.baseline is not None and ratio <= self.baseline + OPEN_MARGIN

    def update(self, ratio):
        """Feed one lip-gap ratio (None = no face). Returns True when a reminder is due."""
        t = self.now()

        if self.calibrating:
            if ratio is not None:
                self._calib_samples.append(ratio)
            if t - self._calib_started >= CALIBRATION_SECONDS and len(self._calib_samples) >= 5:
                self.baseline = statistics.median(self._calib_samples)
                self._calib_started = None
            return False

        if self.baseline is None:
            return False

        if ratio is None:
            # Brief tracking dropouts are ignored; leaving the desk resets the streak.
            if t - self.last_face > FACE_LOST_SECONDS:
                self.window.clear()
            return False
        self.last_face = t

        self.window.append((t, ratio > self.baseline + OPEN_MARGIN))
        while self.window and t - self.window[0][0] > WINDOW_SECONDS:
            self.window.popleft()

        # Frames arrive slightly more than 1/FPS apart, so allow ~1.5 frames of slack.
        covered = t - self.window[0][0]
        if covered < WINDOW_SECONDS - 1.5 / FPS:
            return False
        open_share = sum(1 for _, o in self.window if o) / len(self.window)
        if open_share >= OPEN_FRACTION and t - self.last_alert >= COOLDOWN_SECONDS:
            self.last_alert = t
            self.window.clear()
            return True
        return False


def load_settings():
    try:
        with open(CALIBRATION_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    settings = load_settings()
    settings[key] = value
    with open(CALIBRATION_PATH, "w") as f:
        json.dump(settings, f)


def pick_camera(cameras, preferred_name):
    """Index of the saved camera if it's still connected, else the built-in one, else 0."""
    names = [name for name, _, _ in cameras]
    if preferred_name in names:
        return names.index(preferred_name)
    for i, (_, _, built_in) in enumerate(cameras):
        if built_in:
            return i
    return 0


class BreathingApp(rumps.App):
    def __init__(self):
        super().__init__("Breathing", title="🤐", quit_button=None)
        self.status_item = rumps.MenuItem("Starting…")
        self.toggle_item = rumps.MenuItem("Pause", callback=self.toggle)

        settings = load_settings()
        self.cameras = list_cameras()
        self.camera_index = pick_camera(self.cameras, settings.get("camera"))
        self.switch_camera = threading.Event()
        self.camera_menu = rumps.MenuItem("Camera")
        for i, (name, _, _) in enumerate(self.cameras):
            item = rumps.MenuItem(name, callback=self.choose_camera)
            item.state = int(i == self.camera_index)
            self.camera_menu.add(item)

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            rumps.MenuItem("Recalibrate (keep mouth closed)", callback=self.recalibrate),
            rumps.MenuItem("Show test reminder", callback=self.test_reminder),
            self.camera_menu,
            None,
            rumps.MenuItem("Quit", callback=self.quit),
        ]

        self.detector = MouthDetector(baseline=settings.get("baseline"))
        self.reminders_today = 0
        self.status = "Starting…"
        self.running = threading.Event()
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self._was_calibrating = False
        self.banner = ReminderBanner()
        self.banner_wanted = False   # set by the camera thread, drawn by the main thread
        self.test_banner_until = 0.0

        if self.detector.baseline is None:
            self._begin_calibration()

        self.running.set()
        self.worker = threading.Thread(target=self._camera_loop, daemon=True)
        self.worker.start()
        rumps.Timer(self._refresh_menu, 1).start()
        rumps.Timer(self._refresh_banner, 0.25).start()

    # --- Menu actions -------------------------------------------------------
    def toggle(self, sender):
        if self.running.is_set():
            self.running.clear()  # camera loop releases the camera -> light turns off
            sender.title = "Resume"
            self.title = "⏸"
        else:
            self.running.set()
            sender.title = "Pause"
            self.title = "🤐"

    def choose_camera(self, sender):
        names = [name for name, _, _ in self.cameras]
        self.camera_index = names.index(sender.title)
        for item in self.camera_menu.values():
            item.state = int(item.title == sender.title)
        save_setting("camera", sender.title)
        self.switch_camera.set()  # camera loop reopens with the new index

    def test_reminder(self, _):
        self.test_banner_until = time.monotonic() + 3

    def recalibrate(self, _):
        self._begin_calibration()
        if not self.running.is_set():
            self.toggle(self.toggle_item)

    def quit(self, _):
        self.stop_flag.set()
        self.running.set()  # wake the worker so it can exit
        self.worker.join(timeout=2)
        rumps.quit_application()

    def _begin_calibration(self):
        with self.lock:
            self.detector.start_calibration()

    # --- Background camera loop ---------------------------------------------
    def _camera_loop(self):
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        if not ensure_access():
            self.status = "No camera access (System Settings → Privacy → Camera)"
            log("camera access denied")
            return
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            while not self.stop_flag.is_set():
                self.running.wait()
                if self.stop_flag.is_set():
                    break
                self.switch_camera.clear()
                name, unique_id, _ = self.cameras[self.camera_index]
                log("opening camera: {}".format(name))
                try:
                    cam = Camera(unique_id)
                except RuntimeError as error:
                    self.status = "Can't open {}: {}".format(name, error)
                    log(self.status)
                    self.switch_camera.wait(5)
                    continue
                try:
                    self._process_frames(cam, landmarker)
                finally:
                    cam.release()
                    self.banner_wanted = False
                    if not self.stop_flag.is_set():
                        self.status = "Paused (camera off)"

    def _process_frames(self, cam, landmarker):
        start = time.monotonic()
        first_frame = True
        while self.running.is_set() and not self.stop_flag.is_set() and not self.switch_camera.is_set():
            tick = time.monotonic()
            frame = cam.read()
            if frame is None:
                self.status = "Camera isn't sending frames"
                continue
            if first_frame:
                log("camera started ({}x{})".format(frame.shape[1], frame.shape[0]))
                first_frame = False

            if frame.shape[1] > ANALYSIS_WIDTH:  # full HD is overkill for finding lips
                height = int(frame.shape[0] * ANALYSIS_WIDTH / frame.shape[1])
                frame = cv2.resize(frame, (ANALYSIS_WIDTH, height), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect_for_video(image, int((tick - start) * 1000))
            ratio = lip_gap_ratio(result.face_landmarks[0]) if result.face_landmarks else None

            with self.lock:
                alert = self.detector.update(ratio)
                calibrating = self.detector.calibrating

            if self._was_calibrating and not calibrating:
                save_setting("baseline", self.detector.baseline)
            self._was_calibrating = calibrating

            if alert:
                self.reminders_today += 1
                self.banner_wanted = True
                log("reminder shown (gap {:.3f})".format(ratio))
            elif self.banner_wanted and self.detector.mouth_closed(ratio):
                self.banner_wanted = False
                log("reminder cleared")

            if calibrating:
                self.status = "Calibrating… keep mouth closed"
            elif ratio is not None and self.detector.baseline is not None:
                threshold = self.detector.baseline + OPEN_MARGIN
                self.status = "Mouth {} (gap {:.3f}, open above {:.3f})".format(
                    "OPEN" if ratio > threshold else "closed", ratio, threshold)
            elif ratio is None:
                self.status = "No face in view"
            else:
                self.status = "Monitoring"

            remaining = 1.0 / FPS - (time.monotonic() - tick)
            if remaining > 0:
                time.sleep(remaining)

    def _refresh_banner(self, _):
        if self.banner_wanted or time.monotonic() < self.test_banner_until:
            self.banner.show()
        else:
            self.banner.hide()

    def _refresh_menu(self, _):
        self.status_item.title = "{} · reminders: {}".format(self.status, self.reminders_today)


if __name__ == "__main__":
    BreathingApp().run()
