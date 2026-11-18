import asyncio
import csv
import importlib
import io
import os
import re
import signal
import sqlite3
import threading
import time
import traceback
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

import cv2
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config


def _import(attr, *module_names):
    
    for name in module_names:
        try:
            return getattr(importlib.import_module(name), attr)
        except ModuleNotFoundError as e:
            if e.name != name:  
                raise
    raise ImportError(f"Could not find {attr} in any of: {module_names}")


FaceDetector = _import("FaceDetector", "detector", "Detector")
FaceEncoder = _import("FaceEncoder", "encoder", "Encoder")
FaceMatcher = _import("FaceMatcher", "matcher", "Matcher")
FaceDatabase = _import("FaceDatabase", "database", "Database")


FRAME_WIDTH = getattr(config, "FRAME_WIDTH", 640)
FRAME_HEIGHT = getattr(config, "FRAME_HEIGHT", 480)
PROCESS_EVERY_N_FRAMES = getattr(config, "PROCESS_EVERY_N_FRAMES", 3)
SCALE = 0.25 

# Attendance
# recognised in several processed frames
ATTENDANCE_COOLDOWN = getattr(config, "ATTENDANCE_COOLDOWN", 300)            # seconds
ATTENDANCE_CONFIRM_FRAMES = getattr(config, "ATTENDANCE_CONFIRM_FRAMES", 3)  # processed frames

# FACE_VIDEO_SOURCE 
_src = os.environ.get("FACE_VIDEO_SOURCE")
if _src is None:
    VIDEO_SOURCE = getattr(config, "CAMERA_INDEX", 0)
elif _src.isdigit():
    VIDEO_SOURCE = int(_src)
else:
    VIDEO_SOURCE = _src

STATIC_DIR = Path(__file__).resolve().parent / "static"
PHOTOS_DIR = Path(config.DB_PATH).parent / "photos"   # one snapshot per enrolled student
NAME_RE = re.compile(r"^[\w .\-]{1,40}$")


def photo_path(name):
    """Stable file name for a student's snapshot photo."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "student"
    return PHOTOS_DIR / (slug + ".jpg")


class AttendanceLog:
    """Attendance rows live in the same SQLite file as the faces table."""

    def __init__(self, db_path, cooldown, confirm_frames):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cooldown = cooldown
        self.confirm_frames = confirm_frames
        self.seq = 0             
        self._streak = {}        # name -> consecutive processed frames recognised
        self._last_logged = {}   # name -> epoch of that person's latest entry
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    epoch REAL NOT NULL
                )
                """
            )

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            with conn:  
                yield conn
        finally:
            conn.close()

    def observe(self, names):
        
        present = set(names)
        for gone in [n for n in self._streak if n not in present]:
            del self._streak[gone]

        now = time.time()
        for name in present:
            self._streak[name] = self._streak.get(name, 0) + 1
            if self._streak[name] < self.confirm_frames:
                continue
            last = self._last_logged.get(name)
            if last is None:  
                last = self._last_logged[name] = self._last_epoch(name)
            if now - last >= self.cooldown:
                self._insert(name, now)

    def _last_epoch(self, name):
        with self._connect() as conn:
            row = conn.execute("SELECT MAX(epoch) FROM attendance WHERE name = ?", (name,)).fetchone()
        return row[0] or 0.0

    def _insert(self, name, epoch):
        ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute("INSERT INTO attendance (name, ts, epoch) VALUES (?, ?, ?)", (name, ts, epoch))
        self._last_logged[name] = epoch
        self.seq += 1

    def recent(self, limit=100):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, ts FROM attendance ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"name": n, "time": t} for n, t in rows]

    def present_today(self):
        start = datetime.now().strftime("%Y-%m-%d") + " 00:00:00"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name FROM attendance WHERE ts >= ? ORDER BY name", (start,)
            ).fetchall()
        return [r[0] for r in rows]

    def all_rows(self):
        with self._connect() as conn:
            return conn.execute("SELECT name, ts FROM attendance ORDER BY id ASC").fetchall()

    def rows_for(self, name):
        """All check-in timestamps for one person, newest first."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT ts FROM attendance WHERE name = ? ORDER BY id DESC", (name,)
            ).fetchall()

    def clear(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM attendance")
        self._last_logged = {}  
        self.seq += 1


class RecognitionWorker:
    def __init__(self):
        self.detector = FaceDetector()
        self.encoder = FaceEncoder()
        self.matcher = FaceMatcher()
        self.db = FaceDatabase()
        self.attendance = AttendanceLog(config.DB_PATH, ATTENDANCE_COOLDOWN, ATTENDANCE_CONFIRM_FRAMES)

        self.model_lock = threading.Lock()  
        self.frame_lock = threading.Lock()   
        self.enroll_lock = threading.Lock()  

        self.known = ([], [])                
        self.raw_frame = None                
        self.jpeg = None                     
        self.frame_id = 0
        self.faces = []                      
        self.error = None                   
        self.proc_error = None               

        self._stop = threading.Event()
        self._cam_enabled = threading.Event()
        self._cam_enabled.set()             
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.reload()

    # ---------- lifecycle ----------
    def start(self):
        self._thread.start()

    def begin_shutdown(self):
        self._stop.set()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=3)

    @property
    def stopped(self):
        return self._stop.is_set()

    @property
    def camera_enabled(self):
        return self._cam_enabled.is_set()

    @property
    def current_error(self):
        return self.error or self.proc_error

    def set_camera(self, on):
        if on:
            self.error = None
            self.proc_error = None
            self._cam_enabled.set()
        else:
            self._cam_enabled.clear()

    def reload(self):
        names, encodings = self.db.load_all()
        self.known = (names, encodings)  

    # recognition
    def _process(self, frame):
        names, encodings = self.known
        small = cv2.resize(frame, (0, 0), fx=SCALE, fy=SCALE)
        with self.model_lock:
            locations = self.detector.detect_faces(small)
            face_encodings = self.encoder.encode_faces(small, locations)

        inv = round(1 / SCALE)
        results = []
        for (top, right, bottom, left), enc in zip(locations, face_encodings):
            name, dist = self.matcher.match(enc, encodings, names)
            results.append({
                "box": (top * inv, right * inv, bottom * inv, left * inv),
                "name": name,
                "distance": None if dist is None else float(dist),
            })
        return results

    @staticmethod
    def _draw(frame, results):
        for r in results:
            top, right, bottom, left = r["box"]
            known = r["name"] != "Unknown"
            color = (0, 200, 0) if known else (0, 0, 255)
            label = r["name"] if r["distance"] is None else f'{r["name"]} ({r["distance"]:.2f})'
            cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
            cv2.rectangle(frame, (left, bottom - 28), (right, bottom), color, cv2.FILLED)
            cv2.putText(frame, label, (left + 6, bottom - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    def _clear_frame(self):
        with self.frame_lock:
            self.raw_frame = None
            self.jpeg = None
            self.faces = []

    def _loop(self):
        while not self._stop.is_set():
            if not self._cam_enabled.is_set():      
                self._clear_frame()
                self._stop.wait(0.2)
                continue

            cap = cv2.VideoCapture(VIDEO_SOURCE)
            if not cap.isOpened():
                cap.release()
                self.error = "Could not open the camera. Is another program using it?"
                self._cam_enabled.clear()            
                continue
            try:
                self._capture(cap)
            finally:
                cap.release()                        
                self._clear_frame()

    def _capture(self, cap):
        if isinstance(VIDEO_SOURCE, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

        n = 0
        results = []
        while not self._stop.is_set() and self._cam_enabled.is_set():
            ok, frame = cap.read()
            if not ok:
                if isinstance(VIDEO_SOURCE, str):  
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                self.error = "Camera stopped returning frames."
                time.sleep(0.5)
                continue
            self.error = None

            if n % PROCESS_EVERY_N_FRAMES == 0:
                try:
                    results = self._process(frame)
                    self.attendance.observe([r["name"] for r in results if r["name"] != "Unknown"])
                    self.proc_error = None
                except Exception as e:  
                    msg = f"Recognition error: {e}"
                    if msg != self.proc_error:
                        traceback.print_exc()
                    self.proc_error = msg
                    results = []
            n += 1

            annotated = frame.copy()
            self._draw(annotated, results)
            encoded_ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])

            with self.frame_lock:
                self.raw_frame = frame
                self.faces = [{"name": r["name"], "distance": r["distance"]} for r in results]
                if encoded_ok:
                    self.jpeg = buf.tobytes()
                    self.frame_id += 1

    # enrollment
    def enroll(self, name, replace=False, shots=5, timeout=10.0):
        """Grab up to `shots` frames that contain exactly one face and store their encodings."""
        collected = []
        deadline = time.time() + timeout
        photo_saved = False
        while (len(collected) < shots and time.time() < deadline
               and self.camera_enabled and not self.stopped):
            with self.frame_lock:
                frame = None if self.raw_frame is None else self.raw_frame.copy()
            if frame is not None:
                with self.model_lock:
                    locations = self.detector.detect_faces(frame)  # full-res, like enroll.py
                    if len(locations) == 1:
                        collected.extend(self.encoder.encode_faces(frame, locations))
                        # keep one snapshot so the UI can show the student's face later
                        if not photo_saved:
                            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
                            if ok:
                                PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
                                photo_path(name).write_bytes(buf.tobytes())
                                photo_saved = True
            time.sleep(0.4)

        if not collected:
            return 0
        if replace:
            self.db.delete_face(name)
        for enc in collected:
            self.db.add_face(name, enc)
        self.reload()
        return len(collected)


worker = RecognitionWorker()


@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="Face Recognition", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def require_fetch(request: Request):
    """Changing things must come from our own page. A custom header makes browsers
    block other websites from triggering these calls behind your back."""
    if request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Missing X-Requested-With header.")


guard = [Depends(require_fetch)]


class EnrollRequest(BaseModel):
    name: str
    class_name: str = ""
    year: str = ""
    roll_no: str = ""
    replace: bool = False


def _clean_name(raw):
    name = " ".join(raw.split())
    if not NAME_RE.match(name):
        raise HTTPException(422, "Name must be 1-40 characters: letters, digits, spaces, '.', '-' or '_'.")
    return name


def _clean_field(raw, label, limit=40):
    field = " ".join((raw or "").split())
    if len(field) > limit:
        raise HTTPException(422, f"{label} must be at most {limit} characters.")
    return field


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "students.html")


@app.get("/camera")
def camera_page():
    return FileResponse(STATIC_DIR / "camera.html")


@app.get("/enroll")
def enroll_page():
    return FileResponse(STATIC_DIR / "enroll.html")


_server = None  


def _shutting_down():
    return worker.stopped or (_server is not None and _server.should_exit)


async def _mjpeg(request: Request):
    last = -1
    while not _shutting_down():
        if await request.is_disconnected():
            break
        with worker.frame_lock:
            fid, jpg = worker.frame_id, worker.jpeg
        if jpg is None or fid == last:   # camera off, or no new frame yet
            await asyncio.sleep(0.03)
            continue
        last = fid
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"


@app.get("/video_feed")
def video_feed(request: Request):
    return StreamingResponse(_mjpeg(request), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/status")
def status():
    with worker.frame_lock:
        faces = list(worker.faces)
        have_frame = worker.jpeg is not None
    err = worker.current_error
    return {
        "camera_enabled": worker.camera_enabled,
        "camera_ok": worker.camera_enabled and have_frame and err is None,
        "error": err,
        "faces": faces,
        "log_seq": worker.attendance.seq,
    }


# camera and app control
@app.post("/api/camera/start", dependencies=guard)
def camera_start():
    worker.set_camera(True)
    return {"camera_enabled": True}


@app.post("/api/camera/stop", dependencies=guard)
def camera_stop():
    worker.set_camera(False)
    return {"camera_enabled": False}


@app.post("/api/quit", dependencies=guard)
def quit_app():
    worker.begin_shutdown()  

    def _exit_soon():
        time.sleep(0.5)  
        signal.raise_signal(signal.SIGINT) 

    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"quitting": True}


#people
@app.get("/api/people")
def people():
    return [{"name": n, "samples": c} for n, c in worker.db.list_people()]


@app.post("/api/enroll", dependencies=guard)
def enroll(req: EnrollRequest):
    name = _clean_name(req.name)
    class_name = _clean_field(req.class_name, "Class")
    year = _clean_field(req.year, "Year")
    roll_no = _clean_field(req.roll_no, "Roll no")
    if not worker.camera_enabled:
        raise HTTPException(409, "The camera is off. Start the camera first.")
    if worker.current_error or worker.raw_frame is None:
        raise HTTPException(503, worker.current_error or "Camera is not ready yet. Try again in a moment.")
    if not worker.enroll_lock.acquire(blocking=False):
        raise HTTPException(409, "Another enrollment is already in progress.")
    try:
        count = worker.enroll(name, replace=req.replace)
    finally:
        worker.enroll_lock.release()
    if count == 0:
        if not worker.camera_enabled:
            raise HTTPException(409, "The camera was turned off during enrollment.")
        raise HTTPException(422, "No single clear face found. Face the camera alone, in good light, and try again.")
    worker.db.upsert_student(name, class_name, year, roll_no)
    return {"name": name, "enrolled": count}


@app.get("/api/people/{name}/photo")
def person_photo(name: str):
    p = photo_path(name)
    if not p.exists():
        raise HTTPException(404, "No photo stored for this student.")
    return FileResponse(p, media_type="image/jpeg")


@app.delete("/api/people/{name}", dependencies=guard)
def delete_person(name: str):
    if name not in [n for n, _ in worker.db.list_people()]:
        raise HTTPException(404, f"{name} is not enrolled.")
    worker.db.delete_face(name)
    worker.db.delete_student(name)
    photo_path(name).unlink(missing_ok=True)
    worker.reload()
    return {"deleted": name}


# students
@app.get("/api/students")
def students():
    return worker.db.list_students()


@app.get("/api/students/{name}")
def student_detail(name: str):
    if name not in [n for n, _ in worker.db.list_people()]:
        raise HTTPException(404, f"{name} is not enrolled.")
    s = worker.db.get_student(name) or {"name": name, "class_name": "", "year": "", "roll_no": ""}
    entries = [r[0] for r in worker.attendance.rows_for(name)]
    dates = sorted({ts[:10] for ts in entries}, reverse=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "name": name,
        "class_name": s["class_name"],
        "year": s["year"],
        "roll_no": s["roll_no"],
        "present_today": today in dates,
        "days_present": dates,
        "total_checkins": len(entries),
        "entries": entries[:30],
        "entries_hidden": max(0, len(entries) - 30),
    }


# attendance
@app.get("/api/attendance")
def attendance(limit: int = Query(100, ge=1, le=1000)):
    return {
        "entries": worker.attendance.recent(limit),
        "present_today": worker.attendance.present_today(),
    }


def _csv_safe(value):
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


@app.get("/api/attendance.csv")
def attendance_csv():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Time"])
    for name, ts in worker.attendance.all_rows():
        writer.writerow([_csv_safe(name), ts])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM so Excel reads non-English names right
    filename = f"attendance_{datetime.now():%Y-%m-%d}.csv"
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/attendance", dependencies=guard)
def attendance_clear():
    worker.attendance.clear()
    return {"cleared": True}


if __name__ == "__main__":
    import uvicorn

    _server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8000, timeout_graceful_shutdown=3)
    )
    try:
        _server.run()
    except KeyboardInterrupt:  
        pass