#!/usr/bin/env python3
"""Web-based PID control panel for NUEDC car.

Backend: FastAPI + WebSocket. Frontend: tools/pid/web (static files).

Transport is anything pyserial.serial_for_url accepts:
  /dev/ttyACM0          USB CDC
  COM3                  Windows
  socket://1.2.3.4:3333 ESP32 Wi-Fi UART bridge
  rfc2217://host:port   RFC2217 server
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import queue
import statistics
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import serial
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

PID_CHANNELS = ("L", "R", "LINE", "ANG")
RAW_SENSOR_COUNT = 7
HISTORY_LEN = 600  # ~6 s @ 100 Hz

STATE_NAME = {0: "IDLE", 1: "READY", 2: "RUN", 3: "STOP", 4: "ERROR"}

CSV_HEADER = [
    "kind", "ts_ms", "ch", "sp", "meas", "out", "err", "integ", "deriv",
    "p_term", "i_term", "d_term", "raw_out",
    "line_bias", "contrast", "strength", "on_line",
    "raw0", "raw1", "raw2", "raw3", "raw4", "raw5", "raw6",
    "mission", "state", "loop", "seg", "x", "y", "theta_deg", "seg_cm", "mission_time_ms",
    "param", "param_value", "param_min", "param_max", "raw_line",
]
BLACKBOX_HEADER = [
    "idx", "mission_time_ms", "mission", "state", "loop", "seg",
    "x", "y", "theta_deg", "seg_cm",
    "sp_l", "meas_l", "out_l", "sp_r", "meas_r", "out_r",
    "line_bias", "contrast", "strength", "on_line",
]


def _f(s: str) -> float:
    """Tolerant float: empty / 'nan' / 'inf' become 0.0.
    Firmware built with newlib-nano without -u _printf_float prints
    nothing for %f, leaving empty fields between commas."""
    s = s.strip()
    if not s:
        return 0.0
    try:
        v = float(s)
        if v != v or v in (float("inf"), float("-inf")):
            return 0.0
        return v
    except ValueError:
        return 0.0


def _i(s: str) -> int:
    s = s.strip()
    if not s:
        return 0
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return 0


def parse_pid(line: str):
    if not line.startswith("$P,"):
        return None
    parts = line.split(",")
    if len(parts) < 13:
        return None
    try:
        return {
            "kind": "pid", "ts_ms": _i(parts[1]), "ch": parts[2],
            "sp": _f(parts[3]), "meas": _f(parts[4]), "out": _f(parts[5]),
            "err": _f(parts[6]), "integ": _f(parts[7]), "deriv": _f(parts[8]),
            "p": _f(parts[9]), "i": _f(parts[10]),
            "d": _f(parts[11]), "raw_out": _f(parts[12]),
        }
    except Exception:
        return None


def parse_line(line: str):
    if not line.startswith("$L,"):
        return None
    parts = line.split(",")
    if len(parts) < 13:
        return None
    try:
        return {
            "kind": "line", "ts_ms": _i(parts[1]), "bias": _f(parts[2]),
            "contrast": _i(parts[3]), "strength": _i(parts[4]), "on_line": _i(parts[5]),
            "raw": [_i(v) for v in parts[6:13]],
        }
    except Exception:
        return None


def parse_app(line: str):
    if not line.startswith("$A,"):
        return None
    parts = line.split(",")
    if len(parts) < 11:
        return None
    try:
        return {
            "kind": "app", "ts_ms": _i(parts[1]), "mission": _i(parts[2]),
            "state": _i(parts[3]), "loop": _i(parts[4]), "seg": _i(parts[5]),
            "x": _f(parts[6]), "y": _f(parts[7]), "theta_deg": _f(parts[8]),
            "seg_cm": _f(parts[9]), "mission_time_ms": _i(parts[10]),
            "state_name": STATE_NAME.get(_i(parts[3]), str(parts[3])),
        }
    except Exception:
        return None


def parse_gain(line: str):
    if not line.startswith("$G,"):
        return None
    parts = line.split(",")
    if len(parts) < 5:
        return None
    try:
        return {"kind": "gain", "ch": parts[1], "kp": _f(parts[2]),
                "ki": _f(parts[3]), "kd": _f(parts[4])}
    except Exception:
        return None


def parse_cfg(line: str):
    if not line.startswith("$C,"):
        return None
    parts = line.split(",")
    if len(parts) < 5:
        return None
    try:
        return {"kind": "cfg", "param": parts[1], "value": _f(parts[2]),
                "min": _f(parts[3]), "max": _f(parts[4])}
    except Exception:
        return None


def parse_blackbox(line: str):
    if not line.startswith("$B,"):
        return None
    parts = line.split(",")
    if len(parts) < 21:
        return None
    try:
        vals = [
            _i(parts[1]), _i(parts[2]), _i(parts[3]), _i(parts[4]), _i(parts[5]), _i(parts[6]),
            _f(parts[7]), _f(parts[8]), _f(parts[9]), _f(parts[10]),
            _f(parts[11]), _f(parts[12]), _i(parts[13]),
            _f(parts[14]), _f(parts[15]), _i(parts[16]),
            _f(parts[17]), _i(parts[18]), _i(parts[19]), _i(parts[20]),
        ]
        return dict(zip(BLACKBOX_HEADER, vals))
    except Exception:
        return None


class SerialBus:
    """Read/write a pyserial-compatible URL on a background thread."""

    def __init__(self, on_line, on_status):
        self.on_line = on_line
        self.on_status = on_status
        self.ser: serial.Serial | None = None
        self.thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.url: str | None = None
        self.baud: int = 115200
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.ser is not None

    def connect(self, url: str, baud: int):
        with self._lock:
            if self.ser is not None:
                self.disconnect_locked()
            self.url = url
            self.baud = baud
            self.stop_evt.clear()
            self.ser = serial.serial_for_url(url, baud, timeout=0.2)
            self.thread = threading.Thread(target=self._reader, daemon=True)
            self.thread.start()
        self.on_status(f"connected to {url} @ {baud}")

    def disconnect_locked(self):
        self.stop_evt.set()
        if self.thread is not None:
            self.thread.join(timeout=0.5)
            self.thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def disconnect(self):
        with self._lock:
            was = self.ser is not None
            self.disconnect_locked()
        if was:
            self.on_status("disconnected")

    def send(self, text: str):
        with self._lock:
            ser = self.ser
            if ser is None:
                raise RuntimeError("not connected")
            if not text.endswith("\n"):
                text += "\r\n"
            ser.write(text.encode("ascii", errors="ignore"))
            ser.flush()

    def _reader(self):
        assert self.ser is not None
        buf = bytearray()
        ser = self.ser
        while not self.stop_evt.is_set():
            try:
                chunk = ser.read(256)
            except Exception as exc:
                self.on_status(f"serial-error {exc}")
                break
            if not chunk:
                continue
            buf.extend(chunk)
            while b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                txt = line.decode("ascii", errors="ignore").strip()
                if txt:
                    self.on_line(txt)


class State:
    def __init__(self):
        self.t0_ms: int | None = None
        self.channels: dict[str, dict[str, deque]] = {
            ch: {k: deque(maxlen=HISTORY_LEN) for k in ("t", "sp", "meas", "out", "p", "i", "d", "err")}
            for ch in PID_CHANNELS
        }
        self.latest_pid: dict[str, dict] = {}
        self.latest_line: dict | None = None
        self.latest_app: dict | None = None
        self.gains: dict[str, dict] = {ch: {"kp": 0.0, "ki": 0.0, "kd": 0.0} for ch in PID_CHANNELS}
        self.params: dict[str, dict] = {}
        self.blackbox: list[dict] = []
        self.blackbox_active: bool = False
        self.frame_count: int = 0
        self.start_time: float = time.time()
        self.lock = threading.Lock()

    def push_pid(self, rec: dict):
        with self.lock:
            ch = rec["ch"]
            ser = self.channels.get(ch)
            if ser is None:
                return
            if self.t0_ms is None:
                self.t0_ms = rec["ts_ms"]
            t = (rec["ts_ms"] - self.t0_ms) / 1000.0
            ser["t"].append(t)
            for k in ("sp", "meas", "out", "p", "i", "d", "err"):
                ser[k].append(rec[k])
            self.latest_pid[ch] = rec
            self.frame_count += 1

    def push_line(self, rec: dict):
        with self.lock:
            self.latest_line = rec

    def push_app(self, rec: dict):
        with self.lock:
            self.latest_app = rec

    def push_gain(self, rec: dict):
        with self.lock:
            ch = rec["ch"].upper()
            if ch in self.gains:
                self.gains[ch] = {"kp": rec["kp"], "ki": rec["ki"], "kd": rec["kd"]}

    def push_cfg(self, rec: dict):
        with self.lock:
            self.params[rec["param"]] = rec

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            out = {
                "connected": False,
                "frame_count": self.frame_count,
                "uptime": time.time() - self.start_time,
                "channels": {},
                "latest_pid": self.latest_pid.copy(),
                "latest_line": self.latest_line,
                "latest_app": self.latest_app,
                "gains": {ch: g.copy() for ch, g in self.gains.items()},
                "params": {k: v.copy() for k, v in self.params.items()},
                "blackbox_count": len(self.blackbox),
                "blackbox_active": self.blackbox_active,
            }
            for ch, s in self.channels.items():
                out["channels"][ch] = {k: list(v) for k, v in s.items()}
            return out


class CsvLogger:
    def __init__(self):
        self.fh = None
        self.writer = None
        self.path: Path | None = None
        self.lock = threading.Lock()

    def open(self, path: Path):
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "w", newline="")
        self.writer = csv.writer(self.fh)
        self.writer.writerow(CSV_HEADER)
        self.path = path

    def write(self, kind: str, row: dict, raw_line: str = ""):
        with self.lock:
            if self.writer is None:
                return
            base = {h: "" for h in CSV_HEADER}
            base["kind"] = kind
            base["raw_line"] = raw_line
            base.update({k: v for k, v in row.items() if k in base})
            self.writer.writerow([base[h] for h in CSV_HEADER])
            if self.fh:
                self.fh.flush()

    def close(self):
        with self.lock:
            if self.fh is not None:
                try:
                    self.fh.close()
                except Exception:
                    pass
            self.fh = None
            self.writer = None


# ---------------- FastAPI app ----------------

class ConnectReq(BaseModel):
    port: str
    baud: int = 115200


class CmdReq(BaseModel):
    text: str


class PidSetReq(BaseModel):
    ch: str
    kp: float | None = None
    ki: float | None = None
    kd: float | None = None


class CfgSetReq(BaseModel):
    name: str
    value: float


class RateReq(BaseModel):
    hz: int


class RawlineReq(BaseModel):
    on: bool


class AiTuneReq(BaseModel):
    channels: list[str] = Field(default_factory=lambda: ["LINE"])
    seconds: float = 8.0
    aggressiveness: float = 0.5
    apply: bool = False
    provider: str = "local"


class AiAutoReq(BaseModel):
    channels: list[str] = Field(default_factory=lambda: ["LINE"])
    seconds: float = 8.0
    aggressiveness: float = 0.4
    interval: float = 2.0
    max_rounds: int = 12
    provider: str = "local"


def _mean(vals: list[float]) -> float:
    return statistics.fmean(vals) if vals else 0.0


def _stdev(vals: list[float]) -> float:
    return statistics.stdev(vals) if len(vals) >= 2 else 0.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _round_gain(v: float) -> float:
    if abs(v) >= 100:
        return round(v, 2)
    if abs(v) >= 10:
        return round(v, 3)
    return round(v, 5)


def analyze_pid_channel(ch: str, samples: list[dict], gains: dict, aggressiveness: float) -> dict[str, Any]:
    errs = [float(r["err"]) for r in samples]
    outs = [float(r["out"]) for r in samples]
    meas = [float(r["meas"]) for r in samples]
    sps = [float(r["sp"]) for r in samples]
    p_terms = [float(r["p"]) for r in samples]
    i_terms = [float(r["i"]) for r in samples]
    d_terms = [float(r["d"]) for r in samples]

    amp = max(abs(v) for v in errs) if errs else 0.0
    mae = _mean([abs(v) for v in errs])
    bias = _mean(errs)
    sigma = _stdev(errs)
    out_abs = _mean([abs(v) for v in outs])
    out_peak = max(abs(v) for v in outs) if outs else 0.0
    p_abs = _mean([abs(v) for v in p_terms])
    i_abs = _mean([abs(v) for v in i_terms])
    d_abs = _mean([abs(v) for v in d_terms])
    span_meas = (max(meas) - min(meas)) if meas else 0.0
    span_sp = (max(sps) - min(sps)) if sps else 0.0

    sign_changes = 0
    for prev, cur in zip(errs, errs[1:]):
        if prev == 0 or cur == 0:
            continue
        if prev * cur < 0:
            sign_changes += 1

    ratio = (sigma / (mae + 1e-6)) if mae > 1e-6 else 0.0
    oscillating = sign_changes >= max(4, len(errs) // 18) and ratio > 0.75
    steady_bias = abs(bias) > max(0.02, mae * 0.45) and sign_changes <= max(2, len(errs) // 35)
    sluggish = not oscillating and mae > max(0.03, span_sp * 0.25) and out_peak < 850
    saturated = out_peak > 920
    noisy_d = d_abs > max(p_abs * 0.85, 1.0) and sign_changes > 3

    kp = float(gains.get("kp", 0.0))
    ki = float(gains.get("ki", 0.0))
    kd = float(gains.get("kd", 0.0))
    next_gains = {"kp": kp, "ki": ki, "kd": kd}
    notes: list[str] = []

    gain_step = _clamp(float(aggressiveness), 0.1, 1.0)
    small_up = 1.0 + 0.08 * gain_step
    med_up = 1.0 + 0.16 * gain_step
    small_down = 1.0 - 0.08 * gain_step
    med_down = 1.0 - 0.16 * gain_step

    if oscillating:
        next_gains["kp"] *= med_down
        next_gains["kd"] *= small_up if kd > 0 else 1.0
        if ki > 0:
            next_gains["ki"] *= small_down
        notes.append("误差频繁过零且标准差偏高，先压低 Kp；已有 Kd 时略增阻尼。")
    elif sluggish:
        next_gains["kp"] *= med_up
        if kd > 0 and sign_changes <= 2:
            next_gains["kd"] *= small_down
        notes.append("误差收敛偏慢且输出未饱和，优先小幅提高 Kp。")
    elif steady_bias:
        if ki > 0:
            next_gains["ki"] *= small_up
        else:
            base = max(abs(kp) * 0.01, 0.001)
            if ch in ("L", "R"):
                base = max(abs(kp) * 0.015, 0.5)
            next_gains["ki"] = base * gain_step
        notes.append("存在同向稳态误差，建议增加一点 Ki 或检查机械/传感器偏置。")
    else:
        notes.append("当前波形没有明显发散，建议只做小步验证。")

    if saturated:
        next_gains["kp"] *= small_down
        if ki > 0:
            next_gains["ki"] *= small_down
        notes.append("输出接近限幅，参数变化要更保守。")

    if noisy_d and kd > 0:
        next_gains["kd"] *= small_down
        notes.append("D 项占比偏高，略降 Kd 可减少抖动。")

    # Keep zero gains zero unless the rule above deliberately introduced Ki.
    if kp == 0:
        next_gains["kp"] = 0.0
    if kd == 0:
        next_gains["kd"] = 0.0

    for key in next_gains:
        next_gains[key] = _round_gain(max(0.0, next_gains[key]))

    if next_gains == {"kp": _round_gain(kp), "ki": _round_gain(ki), "kd": _round_gain(kd)}:
        if mae > 0.01 and kp > 0:
            next_gains["kp"] = _round_gain(kp * small_up)
            notes.append("没有触发强特征，按保守策略微调 Kp。")

    confidence = "low"
    if len(samples) >= 120:
        confidence = "medium"
    if len(samples) >= 300 and (oscillating or sluggish or steady_bias):
        confidence = "high"

    return {
        "ch": ch,
        "samples": len(samples),
        "metrics": {
            "mae": round(mae, 5),
            "bias": round(bias, 5),
            "sigma": round(sigma, 5),
            "amp": round(amp, 5),
            "sign_changes": sign_changes,
            "out_mean_abs": round(out_abs, 5),
            "out_peak_abs": round(out_peak, 5),
            "meas_span": round(span_meas, 5),
        },
        "flags": {
            "oscillating": oscillating,
            "steady_bias": steady_bias,
            "sluggish": sluggish,
            "saturated": saturated,
            "noisy_d": noisy_d,
        },
        "current": {"kp": _round_gain(kp), "ki": _round_gain(ki), "kd": _round_gain(kd)},
        "suggested": next_gains,
        "notes": notes,
        "confidence": confidence,
    }


def pid_result_is_good(rec: dict) -> bool:
    if rec.get("error"):
        return False
    metrics = rec.get("metrics", {})
    flags = rec.get("flags", {})
    mae = float(metrics.get("mae", 999.0))
    sigma = float(metrics.get("sigma", 999.0))
    out_peak = float(metrics.get("out_peak_abs", 999.0))
    if flags.get("oscillating") or flags.get("sluggish") or flags.get("saturated") or flags.get("noisy_d"):
        return False
    if rec.get("ch") in ("L", "R"):
        return mae < 0.035 and sigma < 0.055 and out_peak < 850
    if rec.get("ch") == "LINE":
        return mae < 0.045 and sigma < 0.075 and out_peak < 0.32
    if rec.get("ch") == "ANG":
        return mae < 2.0 and sigma < 3.0
    return mae < 0.05


def _extract_json_object(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _make_ai_prompt(payload: dict) -> str:
    return """你是一个给 NUEDC 小车调 PID 的控制工程助手。你只能输出 JSON，不能输出 Markdown。

目标：
- 根据最近遥测和 baseline 建议，给每个通道返回下一轮 PID 参数。
- 参数要保守，小步调整；如果数据不足或风险大，保持当前参数。
- 不要修改未请求通道。
- 输出必须是一个 JSON 对象，格式如下：
{
  "channels": {
    "LINE": {"kp": 82.0, "ki": 0.0, "kd": 19.5, "reason": "short reason"}
  },
  "summary": "one sentence",
  "confidence": "low|medium|high"
}

硬性安全规则：
- kp/ki/kd 不能为负数。
- 单轮变化不超过当前值的 20%；如果当前值为 0，只允许 KI 从 0 小幅增加。
- 如果 telemetry 里有 saturated/noisy_d/oscillating 标志，优先保守，不要激进增益。

数据包：
""" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _call_ai_cli(provider: str, prompt: str, timeout_s: float = 25.0) -> tuple[dict | None, str, str]:
    provider = provider.lower()
    if provider == "claude":
        exe = shutil.which("claude")
        if not exe:
            return None, "", "claude command not found"
        cmd = [exe, "-p", prompt, "--output-format", "text"]
    elif provider == "codex":
        exe = shutil.which("codex")
        if not exe:
            return None, "", "codex command not found"
        cmd = [exe, "exec", "-C", str(HERE), "-s", "read-only",
               "-a", "never", "--skip-git-repo-check", "-"]
    else:
        return None, "", f"unknown provider {provider}"

    try:
        if provider == "codex":
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_s)
        else:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, "", f"{provider} timed out"
    except Exception as exc:
        return None, "", str(exc)

    out = proc.stdout.strip()
    err = proc.stderr.strip()
    if proc.returncode != 0:
        return None, out, err or f"{provider} exited {proc.returncode}"
    return _extract_json_object(out), out, err


def _safe_merge_ai_results(local_results: list[dict], ai_obj: dict | None, provider: str) -> list[dict]:
    if not ai_obj:
        return local_results
    channels_obj = ai_obj.get("channels", {})
    if not isinstance(channels_obj, dict):
        return local_results
    merged: list[dict] = []
    for rec in local_results:
        if rec.get("error"):
            merged.append(rec)
            continue
        ch = rec["ch"]
        ai_ch = channels_obj.get(ch)
        if not isinstance(ai_ch, dict):
            merged.append(rec)
            continue
        current = rec["current"]
        suggested = rec["suggested"].copy()
        accepted: dict[str, float] = {}
        for key in ("kp", "ki", "kd"):
            try:
                val = float(ai_ch[key])
            except (KeyError, TypeError, ValueError):
                val = suggested[key]
            cur = float(current[key])
            if cur == 0.0:
                if key == "ki":
                    limit = max(0.001, max(float(current["kp"]) * 0.02, 0.001))
                    val = _clamp(val, 0.0, limit)
                else:
                    val = 0.0
            else:
                val = _clamp(val, cur * 0.8, cur * 1.2)
            accepted[key] = _round_gain(max(0.0, val))
        out = rec.copy()
        out["suggested"] = accepted
        out["provider"] = provider
        out["ai_reason"] = str(ai_ch.get("reason", ai_obj.get("summary", "")))[:240]
        out["notes"] = list(out.get("notes", [])) + [f"{provider} 建议: {out['ai_reason']}"]
        merged.append(out)
    return merged


def make_app(args) -> FastAPI:
    event_queue: queue.Queue[dict] = queue.Queue(maxsize=20000)
    state = State()
    csv_logger = CsvLogger()
    ws_clients: set[WebSocket] = set()
    main_loop: asyncio.AbstractEventLoop | None = None
    ai_auto = {
        "running": False,
        "round": 0,
        "stable_rounds": 0,
        "max_rounds": 0,
        "reason": "",
        "started_at": 0.0,
        "last_results": [],
        "last_sent": [],
        "channels": [],
    }
    ai_auto_stop = threading.Event()
    ai_auto_lock = threading.Lock()
    ai_auto_thread: threading.Thread | None = None

    def push_event(ev: dict):
        try:
            event_queue.put_nowait(ev)
        except queue.Full:
            pass

    def on_line(text: str):
        # parse and update state in worker thread, enqueue for ws broadcast
        rec = parse_pid(text)
        if rec is not None:
            state.push_pid(rec)
            csv_logger.write("pid", rec, raw_line=text)
            push_event({"type": "pid", "data": rec})
            return
        rec = parse_line(text)
        if rec is not None:
            state.push_line(rec)
            row = {**rec, **{f"raw{i}": v for i, v in enumerate(rec["raw"])}}
            row.pop("raw", None)
            csv_logger.write("line", row, raw_line=text)
            push_event({"type": "line", "data": rec})
            return
        rec = parse_app(text)
        if rec is not None:
            state.push_app(rec)
            csv_logger.write("app", rec, raw_line=text)
            push_event({"type": "app", "data": rec})
            return
        rec = parse_gain(text)
        if rec is not None:
            state.push_gain(rec)
            push_event({"type": "gain", "data": rec})
            push_event({"type": "log", "data": {"text": text}})
            return
        rec = parse_cfg(text)
        if rec is not None:
            state.push_cfg(rec)
            csv_logger.write("cfg", rec, raw_line=text)
            push_event({"type": "cfg", "data": rec})
            push_event({"type": "log", "data": {"text": text}})
            return
        rec = parse_blackbox(text)
        if rec is not None:
            with state.lock:
                state.blackbox.append(rec)
                count = len(state.blackbox)
            push_event({"type": "blackbox", "data": {"count": count}})
            return
        if text.startswith("$BHEAD"):
            with state.lock:
                state.blackbox = []
                state.blackbox_active = True
            push_event({"type": "log", "data": {"text": text}})
            push_event({"type": "blackbox", "data": {"count": 0, "head": text}})
            return
        if text.startswith("$BEND"):
            with state.lock:
                state.blackbox_active = False
                count = len(state.blackbox)
            push_event({"type": "log", "data": {"text": text}})
            push_event({"type": "blackbox", "data": {"count": count, "done": True}})
            return
        push_event({"type": "log", "data": {"text": text}})

    def on_status(msg: str):
        push_event({"type": "status", "data": {"text": msg, "connected": bus.connected}})

    bus = SerialBus(on_line, on_status)

    def collect_ai_results(channels: list[str], seconds: float, aggressiveness: float) -> list[dict]:
        seconds = _clamp(float(seconds), 1.0, 30.0)
        results: list[dict] = []
        with state.lock:
            gains_copy = {ch: g.copy() for ch, g in state.gains.items()}
            for ch in channels:
                ser = state.channels.get(ch)
                if ser is None or not ser["t"]:
                    results.append({"ch": ch, "error": "no telemetry"})
                    continue
                t_vals = list(ser["t"])
                newest = t_vals[-1]
                cutoff = newest - seconds
                idx0 = 0
                for idx, t in enumerate(t_vals):
                    if t >= cutoff:
                        idx0 = idx
                        break
                samples = []
                for idx in range(idx0, len(t_vals)):
                    samples.append({
                        "sp": ser["sp"][idx],
                        "meas": ser["meas"][idx],
                        "out": ser["out"][idx],
                        "err": ser["err"][idx],
                        "p": ser["p"][idx],
                        "i": ser["i"][idx],
                        "d": ser["d"][idx],
                    })
                if len(samples) < 20:
                    results.append({"ch": ch, "error": f"not enough samples ({len(samples)})"})
                    continue
                results.append(analyze_pid_channel(ch, samples, gains_copy.get(ch, {}), aggressiveness))
        return results

    def enrich_results_with_provider(results: list[dict], provider: str, seconds: float) -> list[dict]:
        provider = (provider or "local").lower()
        if provider == "local":
            for rec in results:
                if not rec.get("error"):
                    rec["provider"] = "local"
            return results
        payload = {
            "provider": provider,
            "window_seconds": seconds,
            "baseline_results": results,
            "safety": {
                "max_single_round_change_ratio": 0.20,
                "non_negative_gains": True,
                "fallback": "keep baseline if output is invalid",
            },
        }
        ai_obj, raw, err = _call_ai_cli(provider, _make_ai_prompt(payload))
        if ai_obj is None:
            out = []
            for rec in results:
                if rec.get("error"):
                    out.append(rec)
                    continue
                item = rec.copy()
                item["provider"] = "local"
                item["notes"] = list(item.get("notes", [])) + [f"{provider} 不可用，已回退本地建议: {err or raw[:160]}"]
                out.append(item)
            return out
        merged = _safe_merge_ai_results(results, ai_obj, provider)
        for rec in merged:
            if not rec.get("error"):
                rec["ai_summary"] = str(ai_obj.get("summary", ""))[:240]
                rec["ai_confidence"] = str(ai_obj.get("confidence", ""))[:32]
        return merged

    def apply_ai_results(results: list[dict]) -> list[str]:
        sent: list[str] = []
        for rec in results:
            if rec.get("error"):
                continue
            ch = rec["ch"]
            suggested = rec["suggested"]
            current = rec["current"]
            for gain, key in (("KP", "kp"), ("KI", "ki"), ("KD", "kd")):
                val = suggested[key]
                if val == current[key]:
                    continue
                cmd = f"$SET,{ch},{gain},{val:.6g}\r\n"
                bus.send(cmd)
                sent.append(cmd.strip())
                push_event({"type": "log", "data": {"text": f">> {cmd.strip()}"}})
                time.sleep(0.03)
        if sent:
            try:
                bus.send("$DUMP\r\n")
            except Exception:
                pass
        return sent

    def ai_auto_snapshot() -> dict:
        with ai_auto_lock:
            return {
                "running": bool(ai_auto["running"]),
                "round": ai_auto["round"],
                "stable_rounds": ai_auto["stable_rounds"],
                "max_rounds": ai_auto["max_rounds"],
                "reason": ai_auto["reason"],
                "started_at": ai_auto["started_at"],
                "last_results": ai_auto["last_results"],
                "last_sent": ai_auto["last_sent"],
                "channels": ai_auto["channels"],
            }

    def ai_auto_publish(status: str):
        snap = ai_auto_snapshot()
        snap["status"] = status
        push_event({"type": "ai_auto", "data": snap})

    def ai_auto_worker(req: AiAutoReq):
        channels = [ch.upper() for ch in req.channels if ch.upper() in PID_CHANNELS]
        try:
            bus.send(f"$RATE,{args.rate}\r\n")
            time.sleep(0.03)
            bus.send("$RESUME\r\n")
        except Exception as exc:
            with ai_auto_lock:
                ai_auto["running"] = False
                ai_auto["reason"] = f"send failed: {exc}"
            ai_auto_publish("stopped")
            return

        # Let fresh telemetry fill the first sampling window.
        wait_s = _clamp(req.seconds, 1.0, 30.0)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if ai_auto_stop.wait(0.1):
                with ai_auto_lock:
                    ai_auto["running"] = False
                    ai_auto["reason"] = "user stopped"
                ai_auto_publish("stopped")
                return

        reason = "max rounds reached"
        for round_idx in range(1, int(req.max_rounds) + 1):
            if ai_auto_stop.is_set():
                reason = "user stopped"
                break
            try:
                results = collect_ai_results(channels, req.seconds, req.aggressiveness)
                results = enrich_results_with_provider(results, req.provider, req.seconds)
                all_good = bool(results) and all(pid_result_is_good(r) for r in results)
                if all_good:
                    sent: list[str] = []
                else:
                    sent = apply_ai_results(results)
            except Exception as exc:
                results = [{"ch": ",".join(channels), "error": str(exc)}]
                sent = []
                reason = f"error: {exc}"
                with ai_auto_lock:
                    ai_auto["last_results"] = results
                    ai_auto["last_sent"] = sent
                break

            with ai_auto_lock:
                ai_auto["round"] = round_idx
                ai_auto["last_results"] = results
                ai_auto["last_sent"] = sent
                if all_good:
                    ai_auto["stable_rounds"] += 1
                else:
                    ai_auto["stable_rounds"] = 0
                stable_rounds = ai_auto["stable_rounds"]
            ai_auto_publish("running")

            if stable_rounds >= 3:
                reason = "stable for 3 rounds"
                break

            if not sent and not all_good:
                reason = "no safe adjustment"
                break

            sleep_s = _clamp(req.interval, 0.5, 10.0)
            if ai_auto_stop.wait(sleep_s):
                reason = "user stopped"
                break

        with ai_auto_lock:
            ai_auto["running"] = False
            ai_auto["reason"] = reason
        ai_auto_publish("stopped")

    async def broadcaster():
        nonlocal main_loop
        main_loop = asyncio.get_running_loop()
        while True:
            try:
                ev = await asyncio.to_thread(event_queue.get, True, 1.0)
            except queue.Empty:
                continue
            dead = []
            data = json.dumps(ev, ensure_ascii=False)
            for ws in list(ws_clients):
                try:
                    await ws.send_text(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ws_clients.discard(ws)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(broadcaster())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            bus.disconnect()
            csv_logger.close()

    app = FastAPI(lifespan=lifespan, title="NUEDC PID Web Dashboard")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/static/index.html")

    @app.get("/api/snapshot")
    async def get_snapshot():
        snap = state.snapshot()
        snap["connected"] = bus.connected
        snap["port"] = bus.url
        snap["baud"] = bus.baud
        snap["csv_path"] = str(csv_logger.path) if csv_logger.path else None
        snap["ai_auto"] = ai_auto_snapshot()
        return snap

    @app.post("/api/connect")
    async def api_connect(req: ConnectReq):
        try:
            bus.connect(req.port, req.baud)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # auto open CSV log
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_logger.open(LOG_DIR / f"dashboard_{stamp}.csv")
        # send default startup commands, spaced out so the MCU UART RX
        # parser does not drop commands while still processing the
        # previous one. $CFGDUMP is not implemented on firmware side.
        await asyncio.to_thread(time.sleep, 0.05)
        for cmd in (f"$RATE,{args.rate}\r\n", "$RAWLINE,1\r\n", "$LOG,1\r\n",
                    "$RESUME\r\n", "$DUMP\r\n"):
            try:
                bus.send(cmd)
            except Exception:
                break
            await asyncio.to_thread(time.sleep, 0.05)
        return {"ok": True, "csv_path": str(csv_logger.path)}

    @app.post("/api/disconnect")
    async def api_disconnect():
        ai_auto_stop.set()
        bus.disconnect()
        csv_logger.close()
        return {"ok": True}

    @app.post("/api/cmd")
    async def api_cmd(req: CmdReq):
        try:
            bus.send(req.text)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        push_event({"type": "log", "data": {"text": f">> {req.text.strip()}"}})
        return {"ok": True}

    @app.post("/api/pid/set")
    async def api_pid_set(req: PidSetReq):
        ch = req.ch.upper()
        if ch not in PID_CHANNELS:
            return JSONResponse({"ok": False, "error": f"bad channel {ch}"}, status_code=400)
        sent = []
        for gain, val in (("KP", req.kp), ("KI", req.ki), ("KD", req.kd)):
            if val is None:
                continue
            cmd = f"$SET,{ch},{gain},{val:.6g}\r\n"
            try:
                bus.send(cmd)
                sent.append(cmd.strip())
                push_event({"type": "log", "data": {"text": f">> {cmd.strip()}"}})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True, "sent": sent}

    @app.post("/api/cfg/set")
    async def api_cfg_set(req: CfgSetReq):
        cmd = f"$CFGSET,{req.name},{req.value:.6g}\r\n"
        try:
            bus.send(cmd)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        push_event({"type": "log", "data": {"text": f">> {cmd.strip()}"}})
        return {"ok": True}

    @app.post("/api/rate")
    async def api_rate(req: RateReq):
        cmd = f"$RATE,{int(req.hz)}\r\n"
        bus.send(cmd)
        push_event({"type": "log", "data": {"text": f">> {cmd.strip()}"}})
        return {"ok": True}

    @app.post("/api/rawline")
    async def api_rawline(req: RawlineReq):
        cmd = f"$RAWLINE,{1 if req.on else 0}\r\n"
        bus.send(cmd)
        push_event({"type": "log", "data": {"text": f">> {cmd.strip()}"}})
        return {"ok": True}

    @app.post("/api/ai/tune")
    async def api_ai_tune(req: AiTuneReq):
        channels = [ch.upper() for ch in req.channels if ch.upper() in PID_CHANNELS]
        if not channels:
            return JSONResponse({"ok": False, "error": "no valid channels"}, status_code=400)

        now_ms = time.time() * 1000.0
        results = collect_ai_results(channels, req.seconds, req.aggressiveness)
        results = await asyncio.to_thread(enrich_results_with_provider, results, req.provider, req.seconds)

        sent: list[str] = []
        if req.apply:
            try:
                sent = await asyncio.to_thread(apply_ai_results, results)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc), "results": results}, status_code=400)

        push_event({"type": "ai_tune", "data": {"results": results, "applied": bool(req.apply), "sent": sent}})
        return {"ok": True, "results": results, "applied": bool(req.apply), "sent": sent, "server_time_ms": now_ms}

    @app.post("/api/ai/auto/start")
    async def api_ai_auto_start(req: AiAutoReq):
        nonlocal ai_auto_thread
        channels = [ch.upper() for ch in req.channels if ch.upper() in PID_CHANNELS]
        if not channels:
            return JSONResponse({"ok": False, "error": "no valid channels"}, status_code=400)
        if not bus.connected:
            return JSONResponse({"ok": False, "error": "not connected"}, status_code=400)
        with ai_auto_lock:
            if ai_auto["running"]:
                return JSONResponse({"ok": False, "error": "auto tune already running"}, status_code=409)
            ai_auto_stop.clear()
            ai_auto.update({
                "running": True,
                "round": 0,
                "stable_rounds": 0,
                "max_rounds": int(_clamp(req.max_rounds, 1, 50)),
                "reason": "",
                "started_at": time.time(),
                "last_results": [],
                "last_sent": [],
                "channels": channels,
            })
        req.channels = channels
        req.seconds = _clamp(req.seconds, 1.0, 30.0)
        req.aggressiveness = _clamp(req.aggressiveness, 0.1, 1.0)
        req.interval = _clamp(req.interval, 0.5, 10.0)
        req.max_rounds = int(_clamp(req.max_rounds, 1, 50))
        req.provider = (req.provider or "local").lower()
        ai_auto_thread = threading.Thread(target=ai_auto_worker, args=(req,), daemon=True)
        ai_auto_thread.start()
        ai_auto_publish("started")
        return {"ok": True, "auto": ai_auto_snapshot()}

    @app.post("/api/ai/auto/stop")
    async def api_ai_auto_stop():
        ai_auto_stop.set()
        with ai_auto_lock:
            if not ai_auto["running"]:
                ai_auto["reason"] = ai_auto["reason"] or "already stopped"
        ai_auto_publish("stopping")
        return {"ok": True, "auto": ai_auto_snapshot()}

    @app.get("/api/ai/auto/status")
    async def api_ai_auto_status():
        return {"ok": True, "auto": ai_auto_snapshot()}

    @app.post("/api/log/{action}")
    async def api_log(action: str):
        mapping = {
            "enable": "$LOG,1\r\n",
            "disable": "$LOG,0\r\n",
            "clear": "$LOGCLR\r\n",
            "dump": "$LOGDUMP\r\n",
            "reset_pid": "$RST\r\n",
            "pause": "$PAUSE\r\n",
            "resume": "$RESUME\r\n",
            "dump_gains": "$DUMP\r\n",
            "dump_cfg": "$DUMP\r\n",  # firmware $DUMP also emits $C,... cfg lines
        }
        if action not in mapping:
            return JSONResponse({"ok": False, "error": f"unknown action {action}"}, status_code=400)
        try:
            bus.send(mapping[action])
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        push_event({"type": "log", "data": {"text": f">> {mapping[action].strip()}"}})
        return {"ok": True}

    @app.post("/api/blackbox/save")
    async def api_blackbox_save():
        with state.lock:
            recs = list(state.blackbox)
        if not recs:
            return JSONResponse({"ok": False, "error": "no records"}, status_code=400)
        base = csv_logger.path or (LOG_DIR / f"dashboard_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        out_path = base.with_name(base.stem + "_blackbox.csv")
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(BLACKBOX_HEADER)
            for r in recs:
                w.writerow([r[k] for k in BLACKBOX_HEADER])
        return {"ok": True, "path": str(out_path), "rows": len(recs)}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        ws_clients.add(ws)
        try:
            # send initial snapshot
            snap = state.snapshot()
            snap["connected"] = bus.connected
            snap["port"] = bus.url
            snap["baud"] = bus.baud
            snap["csv_path"] = str(csv_logger.path) if csv_logger.path else None
            snap["ai_auto"] = ai_auto_snapshot()
            await ws.send_text(json.dumps({"type": "snapshot", "data": snap}))
            while True:
                # keep open; ignore client messages
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_clients.discard(ws)

    if not WEB_DIR.exists():
        WEB_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR), html=True), name="static")

    return app


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--web-port", type=int, default=8765)
    ap.add_argument("--port", default="/dev/ttyACM0", help="default serial URL shown in UI")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--rate", type=int, default=100)
    ap.add_argument("--open", action="store_true", help="open browser after start")
    ap.add_argument("--autoconnect", action="store_true")
    return ap.parse_args()


def main():
    args = parse_args()
    app = make_app(args)
    # expose defaults to frontend via a tiny endpoint
    @app.get("/api/defaults")
    async def defaults():
        return {"port": args.port, "baud": args.baud, "rate": args.rate,
                "autoconnect": args.autoconnect}

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.web_port}/")).start()

    uvicorn.run(app, host=args.host, port=args.web_port, log_level="warning")


if __name__ == "__main__":
    main()
