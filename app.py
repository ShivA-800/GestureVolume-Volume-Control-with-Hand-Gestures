from flask import Flask, render_template, Response, jsonify
import cv2
import mediapipe as mp
import numpy as np
from collections import deque
import threading
import time

try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume_control = cast(interface, POINTER(IAudioEndpointVolume))
    vol_range = volume_control.GetVolumeRange()
    MIN_VOL = vol_range[0]
    MAX_VOL = vol_range[1]
    VOLUME_AVAILABLE = True
except Exception as e:
    print(f"Volume control not available: {e}")
    VOLUME_AVAILABLE = False
    MIN_VOL, MAX_VOL = -65.0, 0.0

app = Flask(__name__)


mp_hands = mp.solutions.hands
hands = mp_hands.Hands(
    max_num_hands=1,
    min_detection_confidence=0.6,
    min_tracking_confidence=0.6
)
mp_draw = mp.solutions.drawing_utils

class GestureState:
    def __init__(self):
        self.camera_active = False
        self.cap = None
        self.current_gesture = "None"
        self.gesture_quality = "Good"
        self.volume_level = 50
        self.distance = 0
        self.smooth_window = deque(maxlen=5)
        self.hand_detected = False
        self.lock = threading.Lock()

state = GestureState()

MIN_DISTANCE = 20
MAX_DISTANCE = 200

GESTURES = {
    "Pinch": {"range": (0, 30), "action": "Click/Select"},
    "Close": {"range": (30, 60), "action": "Hold/Drag"},
    "Medium": {"range": (60, 110), "action": "Neutral"},
    "Far": {"range": (110, 200), "action": "Zoom+"}
}


def classify_gesture(distance):
    """Classify gesture based on distance"""
    for gesture, info in GESTURES.items():
        min_d, max_d = info["range"]
        if min_d <= distance <= max_d:
            center = (min_d + max_d) / 2.0
            max_offset = (max_d - min_d) / 2.0
            quality_score = 100 - (abs(distance - center) / max_offset) * 50
            
            if quality_score > 80:
                quality = "Excellent"
            elif quality_score > 60:
                quality = "Good"
            else:
                quality = "Fair"
            
            return gesture, info["action"], quality
    
    return "None", "None", "Fair"


def map_distance_to_volume(distance):
    if distance < MIN_DISTANCE:
        distance = MIN_DISTANCE
    if distance > MAX_DISTANCE:
        distance = MAX_DISTANCE
    
    volume_pct = ((distance - MIN_DISTANCE) / (MAX_DISTANCE - MIN_DISTANCE)) * 100
    return int(np.clip(volume_pct, 0, 100))


def set_system_volume(volume_pct):
    if not VOLUME_AVAILABLE:
        return
    
    try:
        volume_db = MIN_VOL + (volume_pct / 100.0) * (MAX_VOL - MIN_VOL)
        volume_control.SetMasterVolumeLevel(volume_db, None)
    except Exception as e:
        print(f"Error setting volume: {e}")


def get_system_volume():
    if not VOLUME_AVAILABLE:
        return 50
    
    try:
        current_db = volume_control.GetMasterVolumeLevel()
        volume_pct = ((current_db - MIN_VOL) / (MAX_VOL - MIN_VOL)) * 100
        return int(np.clip(volume_pct, 0, 100))
    except Exception:
        return 50


def generate_frames():
    tracked_volume = get_system_volume()
    
    while True:
        with state.lock:
            if not state.camera_active or state.cap is None:
                # Create a blank placeholder frame
                placeholder = np.zeros((360, 640, 3), dtype=np.uint8)
                cv2.putText(placeholder, "Camera Inactive", (180, 180),
                           cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                cv2.putText(placeholder, "Click START to begin", (160, 220),
                           cv2.FONT_HERSHEY_SIMPLEX, 0.7, (150, 150, 150), 2)
                
                ret, buffer = cv2.imencode('.jpg', placeholder)
                frame_bytes = buffer.tobytes()
                
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                time.sleep(0.5)
                continue
        
        ret, frame = state.cap.read()
        if not ret:
            time.sleep(0.1)
            continue
        
        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = hands.process(rgb)
        
        h, w = frame.shape[:2]
        
        if result.multi_hand_landmarks:
            hand_landmarks = result.multi_hand_landmarks[0]
            mp_draw.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)
            
            lm = hand_landmarks.landmark
            tx, ty = int(lm[4].x * w), int(lm[4].y * h)
            ix, iy = int(lm[8].x * w), int(lm[8].y * h)
            
            cv2.circle(frame, (tx, ty), 10, (255, 0, 255), -1)
            cv2.circle(frame, (ix, iy), 10, (255, 0, 255), -1)
            cv2.line(frame, (tx, ty), (ix, iy), (0, 255, 0), 3)
            
            px_dist = float(np.hypot(ix - tx, iy - ty))
            state.smooth_window.append(px_dist)
            avg_dist = float(np.mean(state.smooth_window))
            
            gesture, action, quality = classify_gesture(avg_dist)
            
            target_volume = map_distance_to_volume(avg_dist)
            
            if abs(target_volume - tracked_volume) > 2:
                set_system_volume(target_volume)
                tracked_volume = target_volume
            
            with state.lock:
                state.current_gesture = action
                state.gesture_quality = quality
                state.volume_level = tracked_volume
                state.distance = avg_dist
                state.hand_detected = True
            
            bar_height = int((tracked_volume / 100.0) * (h - 100))
            cv2.rectangle(frame, (20, h - 80), (60, h - 80 - bar_height), (0, 255, 0), -1)
            cv2.rectangle(frame, (20, h - 80 - (h - 100)), (60, h - 80), (100, 100, 100), 2)
            
            cv2.putText(frame, f"Volume: {tracked_volume}%", (20, h - 20),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        else:
            with state.lock:
                state.hand_detected = False
                state.current_gesture = "None"
        
        ret, buffer = cv2.imencode('.jpg', frame)
        frame_bytes = buffer.tobytes()
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(),
                   mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/start_camera', methods=['POST'])
def start_camera():
    with state.lock:
        if not state.camera_active:
            state.cap = cv2.VideoCapture(0)
            state.camera_active = True
            state.smooth_window.clear()
    return jsonify({"status": "started"})


@app.route('/stop_camera', methods=['POST'])
def stop_camera():
    with state.lock:
        if state.camera_active:
            state.camera_active = False
            if state.cap:
                state.cap.release()
                state.cap = None
    return jsonify({"status": "stopped"})


@app.route('/status')
def get_status():
    with state.lock:
        return jsonify({
            "camera_active": state.camera_active,
            "hand_detected": state.hand_detected,
            "gesture": state.current_gesture,
            "quality": state.gesture_quality,
            "volume": state.volume_level,
            "distance": round(state.distance, 1)
        })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000, threaded=True)
