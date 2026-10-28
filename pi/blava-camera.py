#!/usr/bin/env python3
"""Blava camera : film automatique, segments, LED ACT et recalage de l'horloge par QR « Blava Sync ».

- Filme dès le démarrage (AUTOSTART=1), en segments de SEGMENT_MIN minutes, dans VIDEO_DIR.
- LED ACT : 2 s allumée / 2 s éteinte pendant le film.
- Pendant le film uniquement, cherche QR_HZ fois par seconde un QR « KS1:<ms UTC> » (app Blava Sync).
  Quand une présentation est validée (3 lectures concordantes) : recale l'horloge si elle ne l'est pas
  encore depuis ce démarrage, corrige les épisodes déjà filmés (clock-episode fix qr), démarre un
  nouveau fichier vidéo à l'heure juste, et fait 3 clignotements rapides de la LED en 1 s.
- Pour chaque vidéo : <video>.time.json (clock-episode) et <video>.frames.csv (horodatage de chaque image).
- Bouton optionnel (BTN_PIN) : appui court = démarrer/arrêter le film, appui long 5 s = éteindre.
Réglages : /etc/default/blava-camera
"""
import json
import logging
import os
import pwd
import signal
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone


def env(name, default, cast=str):
    v = os.environ.get(name, "").strip()
    try:
        return cast(v) if v else default
    except ValueError:
        return default


VIDEO_DIR = env("VIDEO_DIR", "/home/edgar/videos")
VIDEO_OWNER = env("VIDEO_OWNER", "")
WIDTH = env("WIDTH", 1280, int)
HEIGHT = env("HEIGHT", 720, int)
FPS = env("FPS", 30, int)
SENSOR_MODE = env("SENSOR_MODE", "2304:1296")
HDR = env("HDR", 1, int)
BITRATE = env("BITRATE", 7500000, int)
SEGMENT_MIN = env("SEGMENT_MIN", 45, float)
AUTOSTART = env("AUTOSTART", 1, int)
BTN_PIN = env("BTN_PIN", 0, int)
QR_ENABLED = env("QR", 1, int)
QR_HZ = env("QR_HZ", 2.0, float)
QR_LATENCY_MS = env("QR_LATENCY_MS", 70, int)   # milieu de la tranche de 100 ms + latence d'affichage
QR_PREFIX = "KS1:"

STATE_DIR = "/var/lib/clock-sync"
FLAG = "/run/clock-synced"
CLOCK_EPISODE = "/usr/local/bin/clock-episode"
QR_LOG = os.path.join(STATE_DIR, "qr-observations.jsonl")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("blava-camera")


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def owner_ids():
    try:
        p = pwd.getpwnam(VIDEO_OWNER) if VIDEO_OWNER else None
        return (p.pw_uid, p.pw_gid) if p else None
    except KeyError:
        return None


def chown(path):
    ids = owner_ids()
    if ids:
        try:
            os.chown(path, *ids)
        except OSError:
            pass


def episode(action, path):
    if not os.path.exists(CLOCK_EPISODE):
        return
    try:
        subprocess.run([CLOCK_EPISODE, action, path], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        side = path + ".time.json"
        if os.path.exists(side):
            chown(side)
    except Exception as e:
        log.warning("clock-episode %s : %s", action, e)


# --------------------------------------------------------------------------- LED

class Led:
    def __init__(self):
        self.base = next((p for p in ("/sys/class/leds/ACT", "/sys/class/leds/led0")
                          if os.path.isdir(p)), None)
        self.lock = threading.Lock()
        self.recording = False
        self.state = 0
        self._w("trigger", "none")
        self._w("brightness", 0)
        threading.Thread(target=self._loop, daemon=True).start()

    def _w(self, name, value):
        if not self.base:
            return
        try:
            with open(f"{self.base}/{name}", "w") as f:
                f.write(str(value))
        except OSError:
            pass

    def _loop(self):
        self.state, next_flip = 0, 0.0
        while True:
            time.sleep(0.05)
            with self.lock:
                if not self.recording:
                    if self.state:
                        self.state = 0
                        self._w("brightness", 0)
                    continue
                now = time.monotonic()
                if now >= next_flip:
                    self.state ^= 1
                    self._w("brightness", self.state)
                    next_flip = now + 2.0

    def beep3(self):
        """Trois clignotements rapides en 1 seconde : l'heure vient d'être recalée."""
        def run():
            with self.lock:
                # pause du clignotement lent : éteint d'abord pour que les 3 éclairs soient bien distincts
                self._w("brightness", 0); time.sleep(0.3)
                for _ in range(3):
                    self._w("brightness", 1); time.sleep(0.15)
                    self._w("brightness", 0); time.sleep(0.18)
                time.sleep(0.2)
                # reprise immédiate du rythme normal
                self._w("brightness", getattr(self, "state", 0) if self.recording else 0)
        threading.Thread(target=run, daemon=True).start()

    def shutdown_blink(self):
        with self.lock:
            for _ in range(3):
                self._w("brightness", 1); time.sleep(0.1)
                self._w("brightness", 0); time.sleep(0.1)


# --------------------------------------------------------------------- caméra

def set_sensor_hdr(on):
    try:
        from picamera2.devices.imx708 import IMX708
        with IMX708(0) as cam:
            cam.set_sensor_hdr_mode(bool(on))
        log.info("HDR capteur : %s", "activé" if on else "désactivé")
    except Exception as e:
        log.warning("HDR capteur non réglé (%s) : on continue sans", e)


class Recorder:
    def __init__(self, led):
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import FileOutput
        self.FileOutput = FileOutput
        self.led = led
        self.lock = threading.Lock()
        self.recording = False
        self.csv = None
        self.path = None
        self.seg_start = 0.0
        os.makedirs(VIDEO_DIR, exist_ok=True)
        chown(VIDEO_DIR)

        set_sensor_hdr(HDR)
        self.picam2 = Picamera2()
        sw, sh = (int(x) for x in SENSOR_MODE.split(":"))
        frame_us = int(1_000_000 / FPS)
        cfg = self.picam2.create_video_configuration(
            main={"size": (WIDTH, HEIGHT), "format": "YUV420"},
            raw={"size": (sw, sh)},
            controls={"FrameDurationLimits": (frame_us, frame_us)},
            buffer_count=4,
        )
        self.picam2.configure(cfg)
        if "AfMode" in self.picam2.camera_controls:
            try:
                self.picam2.set_controls({"AfMode": 2})  # mise au point continue
            except Exception:
                pass
        self.picam2.pre_callback = self._on_frame
        try:
            self.encoder = H264Encoder(bitrate=BITRATE, repeat=True, iperiod=FPS * 2)
        except TypeError:
            self.encoder = H264Encoder(bitrate=BITRATE)
        self.picam2.start()
        log.info("Caméra prête : %dx%d %d fps, mode capteur %s", WIDTH, HEIGHT, FPS, SENSOR_MODE)

    def _on_frame(self, request):
        f = self.csv
        if f is None:
            return
        try:
            ts = request.get_metadata().get("SensorTimestamp")
            if ts is not None:
                f.write(f"{ts}\n")
        except Exception:
            pass

    def start(self):
        if self.recording:
            return
        name = datetime.now().strftime("video_%Y-%m-%d_%H-%M-%S")
        path = os.path.join(VIDEO_DIR, name + ".h264")
        csv = open(path + ".frames.csv", "w", buffering=1 << 16)
        csv.write("# sensor_timestamp_ns (horloge monotone du Pi), une ligne par image\n")
        csv.write(f"# boot_id={boot_id()}\n")
        episode("start", path)
        self.path = path
        self.picam2.start_encoder(self.encoder, self.FileOutput(path))
        self.csv = csv
        self.seg_start = time.monotonic()
        self.recording = True
        self.led.recording = True
        log.info("Film : %s", path)

    def stop(self):
        if not self.recording:
            return
        self.recording = False
        self.led.recording = False
        try:
            self.picam2.stop_encoder()
        except Exception as e:
            log.warning("arrêt encodeur : %s", e)
        csv, self.csv = self.csv, None
        if csv:
            csv.close()
        episode("stop", self.path)
        for p in (self.path, self.path + ".frames.csv"):
            chown(p)
        subprocess.run(["sync"])
        log.info("Fin du film : %s", self.path)

    def next_segment(self):
        self.stop()
        self.start()

    def capture_luma(self):
        """Une image (plan Y, niveaux de gris) et son horodatage capteur, appariés."""
        req = self.picam2.capture_request()
        try:
            arr = req.make_array("main")
            md = req.get_metadata()
        finally:
            req.release()
        return arr[:HEIGHT, :WIDTH], md.get("SensorTimestamp")

    def close(self):
        self.stop()
        try:
            self.picam2.stop()
            self.picam2.close()
        except Exception:
            pass


# -------------------------------------------------------------------- QR

def parse_qr(text):
    if not text.startswith(QR_PREFIX):
        return None
    try:
        ms = int(text[len(QR_PREFIX):])
    except ValueError:
        return None
    return ms if 1_700_000_000_000 < ms < 4_100_000_000_000 else None


def presentation_offset(readings, now):
    """readings : liste (t_lecture, offset_utc_moins_mono_s, qr_ms). Retourne l'offset validé ou None."""
    recent = [r for r in readings if now - r[0] <= 5.0]
    distinct = {r[2] for r in recent}
    if len(distinct) < 3:
        return None
    offs = [r[1] for r in recent]
    if max(offs) - min(offs) > 0.3:
        return None
    return statistics.median(offs)


def apply_qr_time(offset, recorder, n_reads):
    """offset = UTC - horloge monotone (secondes)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    utc_now = time.monotonic() + offset
    already = os.path.exists(FLAG)
    delta = utc_now - time.time()
    obs = {"boot_id": boot_id(), "mono_to_utc_offset": offset, "utc_iso": datetime.fromtimestamp(utc_now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
           "pi_clock_error_s": round(delta, 3), "readings": n_reads, "video": recorder.path,
           "clock_was_already_synced": already}
    with open(QR_LOG, "a") as f:
        f.write(json.dumps(obs) + "\n")

    if not already:
        subprocess.run(["date", "-u", "-s", "@%.3f" % (time.monotonic() + offset)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        open(FLAG, "w").close()
        if os.path.exists(CLOCK_EPISODE):
            r = subprocess.run([CLOCK_EPISODE, "fix", "qr"], capture_output=True, text=True, timeout=30)
            log.info("clock-episode : %s", r.stdout.strip())
        for unit in ("systemd-timesyncd", "chrony"):
            subprocess.run(["systemctl", "stop", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(STATE_DIR, "history.log"), "a") as f:
            f.write(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} method=qr network=- boot={boot_id()}\n")
        subprocess.run("command -v fake-hwclock >/dev/null && fake-hwclock save force; "
                       "[ -e /dev/rtc0 ] && hwclock -w", shell=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("Horloge recalée par QR (écart corrigé : %+.3f s)", delta)
        # Nouveau fichier, nommé à l'heure juste : ce qui a été filmé avant le QR reste dans le précédent.
        if recorder.recording:
            recorder.next_segment()
    else:
        lvl = log.warning if abs(delta) > 2 else log.info
        lvl("QR lu (horloge déjà recalée ce démarrage, écart mesuré : %+.3f s)", delta)
    recorder.led.beep3()


def qr_loop(recorder):
    from pyzbar.pyzbar import decode, ZBarSymbol
    import numpy as np
    period = 1.0 / max(QR_HZ, 0.2)
    readings, quiet_until = [], 0.0
    log.info("Détection QR active pendant le film (%.1f images/s)", QR_HZ)
    while True:
        t0 = time.monotonic()
        if not recorder.recording or t0 < quiet_until:
            time.sleep(0.5)
            continue
        try:
            luma, ts = recorder.capture_luma()
            found = decode(np.ascontiguousarray(luma), symbols=[ZBarSymbol.QRCODE])
        except Exception as e:
            log.warning("lecture QR : %s", e)
            time.sleep(1)
            continue
        now_ns = time.monotonic_ns()
        if ts is None or abs(now_ns - ts) > 5_000_000_000:
            ts = now_ns - 50_000_000
        for sym in found:
            ms = parse_qr(sym.data.decode("ascii", "ignore"))
            if ms is None:
                continue
            offset = (ms + QR_LATENCY_MS) / 1000.0 - ts / 1e9
            readings.append((time.monotonic(), offset, ms))
        now = time.monotonic()
        readings = [r for r in readings if now - r[0] <= 5.0]
        off = presentation_offset(readings, now)
        if off is not None:
            n = len(readings)
            readings = []
            quiet_until = now + 15.0   # une présentation = un seul signal LED
            try:
                apply_qr_time(off, recorder, n)
            except Exception as e:
                log.error("recalage QR : %s", e)
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


# ------------------------------------------------------------------- main

def main():
    led = Led()
    rec = Recorder(led)
    stop_evt = threading.Event()

    def on_signal(*_):
        stop_evt.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if BTN_PIN:
        from gpiozero import Button
        btn = Button(BTN_PIN, hold_time=5.0)
        held = {"v": False}

        def on_held():
            held["v"] = True
            rec.stop()
            led.shutdown_blink()
            subprocess.run(["sync"])
            subprocess.run(["shutdown", "now"])

        def on_released():
            if held["v"]:
                held["v"] = False
                return
            rec.stop() if rec.recording else rec.start()
        btn.when_held = on_held
        btn.when_released = on_released
        log.info("Bouton actif sur GPIO %d", BTN_PIN)

    if QR_ENABLED:
        threading.Thread(target=qr_loop, args=(rec,), daemon=True).start()
    if AUTOSTART:
        rec.start()

    seg_s = SEGMENT_MIN * 60
    while not stop_evt.is_set():
        stop_evt.wait(1.0)
        if rec.recording and seg_s > 0 and time.monotonic() - rec.seg_start >= seg_s:
            rec.next_segment()
    rec.close()
    log.info("Arrêt propre")


if __name__ == "__main__":
    main()
