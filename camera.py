"""Small AVFoundation webcam reader that opens a camera by its unique ID.

OpenCV numbers cameras in its own order, which doesn't match the names macOS
reports, so picking "FaceTime HD Camera" by index could open a different
camera. Opening the device by ID through AVFoundation avoids that.
"""

import threading
import time

import AVFoundation as AVF
import CoreMedia
import libdispatch
import numpy as np
import objc
import Quartz
from Foundation import NSObject


def list_cameras():
    """(name, unique_id, is_built_in) for each connected camera."""
    devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)
    return [(d.localizedName(), d.uniqueID(), "BuiltIn" in str(d.deviceType())) for d in devices]


def ensure_access(timeout=60):
    """Ask macOS for camera access if it hasn't been decided yet. Returns True if allowed."""
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if status == AVF.AVAuthorizationStatusAuthorized:
        return True
    if status != AVF.AVAuthorizationStatusNotDetermined:
        return False
    answered = threading.Event()
    result = []

    def handler(granted):
        result.append(bool(granted))
        answered.set()

    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVF.AVMediaTypeVideo, handler)
    answered.wait(timeout)
    return bool(result and result[0])


class _FrameSink(NSObject, protocols=[objc.protocolNamed("AVCaptureVideoDataOutputSampleBufferDelegate")]):
    """Receives frames on AVFoundation's queue and keeps only the newest one."""

    def init(self):
        self = objc.super(_FrameSink, self).init()
        if self is None:
            return None
        self.lock = threading.Lock()
        self.frame = None
        self.count = 0
        self.wanted = threading.Event()  # only convert a frame when read() is waiting
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
        if not self.wanted.is_set():
            return
        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            return
        Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
        try:
            width = Quartz.CVPixelBufferGetWidth(pixel_buffer)
            height = Quartz.CVPixelBufferGetHeight(pixel_buffer)
            row_bytes = Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer)
            base = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
            data = np.frombuffer(base.as_buffer(row_bytes * height), dtype=np.uint8)
            bgra = data.reshape(height, row_bytes)[:, : width * 4].reshape(height, width, 4)
            frame = np.ascontiguousarray(bgra[:, :, :3])  # BGRA -> BGR, copied out of the buffer
        finally:
            Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, Quartz.kCVPixelBufferLock_ReadOnly)
        with self.lock:
            self.frame = frame
            self.count += 1
        self.wanted.clear()


def _use_small_format(device, min_width=640):
    """Switch to the smallest landscape format at least min_width wide, at its lowest frame rate.

    The camera otherwise streams full HD at 30 fps, which costs a lot of CPU for
    frames we mostly throw away. Must run after the session has started.
    """
    candidates = []
    for fmt in device.formats():
        dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
        if dims.width >= min_width and dims.width >= dims.height:
            candidates.append((dims.width * dims.height, fmt))
    if not candidates:
        return
    fmt = min(candidates, key=lambda c: c[0])[1]
    ok, _ = device.lockForConfiguration_(None)
    if not ok:
        return
    try:
        device.setActiveFormat_(fmt)
        lowest = min(r.minFrameRate() for r in fmt.videoSupportedFrameRateRanges())
        device.setActiveVideoMinFrameDuration_(CoreMedia.CMTimeMake(1, int(lowest)))
        device.setActiveVideoMaxFrameDuration_(CoreMedia.CMTimeMake(1, int(lowest)))
    finally:
        device.unlockForConfiguration()


class Camera:
    """Streams one camera; read() returns the newest BGR frame as a numpy array."""

    def __init__(self, unique_id):
        device = AVF.AVCaptureDevice.deviceWithUniqueID_(unique_id)
        if device is None:
            raise RuntimeError("camera is not connected")
        device_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if device_input is None:
            raise RuntimeError("can't open camera: {}".format(error))

        session = AVF.AVCaptureSession.alloc().init()
        if not session.canAddInput_(device_input):
            raise RuntimeError("camera is busy or unsupported")
        session.addInput_(device_input)

        output = AVF.AVCaptureVideoDataOutput.alloc().init()
        output.setVideoSettings_({Quartz.kCVPixelBufferPixelFormatTypeKey: Quartz.kCVPixelFormatType_32BGRA})
        output.setAlwaysDiscardsLateVideoFrames_(True)
        self._sink = _FrameSink.alloc().init()
        self._queue = libdispatch.dispatch_queue_create(b"breathing.camera", None)
        output.setSampleBufferDelegate_queue_(self._sink, self._queue)
        session.addOutput_(output)

        session.startRunning()
        _use_small_format(device)  # after start, or the session resets it to full HD
        self._session = session
        self._last_count = 0

    def read(self, timeout=2.0):
        """Wait for a frame newer than the last one returned. None on timeout."""
        deadline = time.monotonic() + timeout
        self._sink.wanted.set()
        while time.monotonic() < deadline:
            with self._sink.lock:
                if self._sink.count != self._last_count:
                    self._last_count = self._sink.count
                    return self._sink.frame
            time.sleep(0.01)
        return None

    def release(self):
        self._session.stopRunning()
