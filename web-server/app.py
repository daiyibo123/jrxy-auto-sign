#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
今日校园自动签到 · Web 服务（多账号版）
=====================================
将 jrxy-engine 签到引擎封装为多账号 Web 管理服务。

账号数据源（与 scripts/batch_submit.py 共用，见 accounts/README.md）：
  accounts/<name>.yml         每账号配置（users 列表只含该账号）
  accounts/<name>.device_id   每账号固定 deviceId（首次运行自动生成；迁移时手工写入原 ID）

启动方式：
  Windows: web-server\\.venv\\Scripts\\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
  Linux  : web-server/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000

环境变量：
  HOST                监听地址，默认 127.0.0.1（对外部署可设 0.0.0.0）
  PORT                监听端口，默认 8000
  WEB_TOKEN           可选；设置后所有接口需携带请求头 X-Web-Token。
                      也可写入 web-server/data/web_token.txt（环境变量优先），内容即令牌，可自定义
  SIGN_TIME           内置定时签到时间（24 小时制 HH:MM），默认 22:05
  AUTO_SIGN           内置定时开关，默认 1=启用 0=停用
  TZ_OFFSET           定时/日志使用的时区（相对 UTC 小时数），默认 8=北京时间，与服务器系统时区无关
  CATCHUP_MINUTES     错过定时点（重启/休眠/对时跳变）后多少分钟内仍补签，默认 120
  NOTIFY_ON_DRY       探测（dry）是否也推送，默认 0（Server酱免费额度有限，留给真实签到）
  JRXY_INTERVAL       批量时每账号间隔秒数，默认 10
  JRXY_RUN_TIMEOUT    单个账号一次执行的最长秒数，默认 300
  RETRY_DELAY_MINUTES 定时签到中“执行异常”的账号隔多少分钟重试，默认 5
  MAX_RETRIES         定时签到中“执行异常”的账号最多重试几次，默认 2（即最多共执行 3 次）
  JRXY_HTTP_TIMEOUT   引擎单个网络请求的读取超时秒数，默认 30
  JRXY_ACCOUNTS_DIR   账号配置目录，默认 <项目根>/accounts

安全提示：
  - 默认仅监听本机；对外部署务必设置 WEB_TOKEN 并启用 HTTPS/反向代理。
  - accounts/*.yml 与 *.device_id 已被 .gitignore 排除，密码绝不回传。
  - 每账号 deviceId 独立固定；已有绑定账号迁移时请在页面填写原 deviceId，
    切勿让其重新生成，否则触发“更换手机频繁”风控。
"""
import base64
import hmac
import json
import logging
import os
import random
import re
import sqlite3
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

# ---------------- 路径 ----------------
ROOT = Path(__file__).resolve().parent.parent          # 项目根目录
ENGINE_DIR = ROOT / "jrxy-engine"                    # 签到引擎目录
WEB_DIR = Path(__file__).resolve().parent
DATA_DIR = WEB_DIR / "data"
STATIC_DIR = WEB_DIR / "static"
ACCOUNTS_DIR = Path(os.getenv("JRXY_ACCOUNTS_DIR", str(ROOT / "accounts")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "signlog.db"
SCHED_PATH = DATA_DIR / "schedule.json"
INDEX_HTML = STATIC_DIR / "index.html"
# 照片池：存放多张候选签到照片，账号可开启“随机选一张”避免每次都用同一张
PHOTO_POOL_DIR = Path(os.getenv("JRXY_PHOTO_POOL_DIR", str(DATA_DIR / "photo_pool")))
PHOTO_POOL_DIR.mkdir(parents=True, exist_ok=True)
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
# 高德地图配置：保存 JS API Key 与安全密钥（留空则不启用地图选点）
AMAP_CONF_PATH = DATA_DIR / "amap.json"
_amap_cache = None      # {"_mtime": float, "conf": dict}，按文件 mtime 缓存

IS_WIN = os.name == "nt"
ENGINE_PY = ENGINE_DIR / (".venv/Scripts/python.exe" if IS_WIN else ".venv/bin/python")
SUBMIT_SCRIPT = ENGINE_DIR / "scripts" / "merged_ios_v4_submit.py"
RANGE_SCRIPT = ENGINE_DIR / "scripts" / "fetch_sign_range.py"
TESTLOGIN_SCRIPT = ENGINE_DIR / "scripts" / "test_login.py"
TOKEN_FILE = DATA_DIR / "web_token.txt"

# ---------------- 运行配置（环境变量） ----------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DEFAULT_SIGN_TIME = os.getenv("SIGN_TIME", "22:05")
DEFAULT_AUTO_SIGN = os.getenv("AUTO_SIGN", "1") == "1"
TZ_OFFSET = float(os.getenv("TZ_OFFSET", "8"))
TZ = timezone(timedelta(hours=TZ_OFFSET))
CATCHUP_MINUTES = int(os.getenv("CATCHUP_MINUTES", "120"))
NOTIFY_ON_DRY = os.getenv("NOTIFY_ON_DRY", "0") == "1"
ACCOUNT_INTERVAL = int(os.getenv("JRXY_INTERVAL", "10"))
RUN_TIMEOUT = int(os.getenv("JRXY_RUN_TIMEOUT", "300"))
RETRY_DELAY_MINUTES = float(os.getenv("RETRY_DELAY_MINUTES", "5"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jrxy-web")


def now() -> datetime:
    """按 TZ_OFFSET（默认北京时间）取当前时间，返回 naive datetime；不依赖服务器系统时区。"""
    return datetime.now(TZ).replace(tzinfo=None)


def load_web_token() -> str:
    tok = os.getenv("WEB_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
        except Exception as e:
            log.warning("读取 %s 失败: %s", TOKEN_FILE, e)
    return tok


WEB_TOKEN = load_web_token()
_run_lock = threading.Lock()

# ---------------- 数据库 ----------------
def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS sign_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                result TEXT NOT NULL,
                detail TEXT NOT NULL
            )"""
        )

def save_log(kind: str, result: str, detail: str):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT INTO sign_log(ts, kind, result, detail) VALUES(?,?,?,?)",
                (now().strftime("%Y-%m-%d %H:%M:%S"), kind, result, detail[-4000:]),
            )
    except Exception as e:
        log.warning("save_log failed: %s", e)

def fetch_logs(limit: int = 50):
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT ts, kind, result, detail FROM sign_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []

def clear_logs():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("DELETE FROM sign_log")

# ---------------- 账号（accounts/ 目录，与 batch_submit.py 共用） ----------------
NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.\u4e00-\u9fa5]{1,32}$")

def valid_account_name(name: str) -> bool:
    return bool(name) and not name.startswith("_") and NAME_RE.match(name) \
        and name not in (".", "..")

def account_paths(name: str):
    return ACCOUNTS_DIR / (name + ".yml"), ACCOUNTS_DIR / (name + ".device_id")

def list_accounts() -> list:
    if not ACCOUNTS_DIR.is_dir():
        return []
    out = []
    for f in sorted(os.listdir(ACCOUNTS_DIR)):
        if f.endswith(".yml") and not f.startswith("_"):
            out.append(f[:-4])
    return out

def read_did(did_file: Path) -> str:
    """读取 deviceId（兼容 BOM）；文件不存在或为空返回 ''。"""
    if did_file.exists():
        try:
            return did_file.read_text(encoding="utf-8-sig").strip()
        except Exception:
            return ""
    return ""

def ensure_device_id(name: str) -> str:
    """与 batch_submit.py 同规则：已存在且非空则沿用，否则生成一次（小写 UUID）。"""
    _, did_file = account_paths(name)
    val = read_did(did_file)
    if val:
        return val
    if did_file.exists():
        raise HTTPException(status_code=400, detail=f"{name}.device_id 内容为空！"
                            "请填入该账号已绑定的 deviceId，或删除该文件让其自动生成（仅限无绑定新账号）")
    did = str(uuid.uuid4())
    did_file.write_text(did, encoding="utf-8")
    return did

def read_account(name: str):
    """返回 (user dict, device_id str)；不存在则 404。"""
    yml, _ = account_paths(name)
    if not valid_account_name(name) or not yml.exists():
        raise HTTPException(status_code=404, detail=f"账号 {name} 不存在")
    try:
        cfg = yaml.safe_load(yml.read_text(encoding="utf-8"))
        user = cfg["users"][0]["user"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"账号 {name} 配置解析失败: {e}")
    _, did_file = account_paths(name)
    return user, read_did(did_file)

def dump_account(user: dict, http_proxy: str = "") -> str:
    # 引擎读取的是 user["httpProxy"]（user 级）；顶层 httpProxy 仅为兼容模板样式保留
    user = dict(user)
    user["httpProxy"] = http_proxy or ""
    cfg = {
        "notifyOption": {"method": 0, "mailApiUrl": ""},
        "ocrOption": {"SecretId": "", "SecretKey": ""},
        "httpProxy": http_proxy or "",
        "users": [{"user": user}],
    }
    return yaml.dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)

def write_account(name: str, user: dict, http_proxy: str = ""):
    yml, _ = account_paths(name)
    yml.write_text(dump_account(user, http_proxy), encoding="utf-8")

def write_temp_account(name: str, user: dict, photo_override: str) -> str:
    """把账号配置写成临时副本（仅覆盖 photo），供引擎本次运行使用。
    文件名以 _ 开头：list_accounts() 会跳过 _ 开头的文件，
    因此不会被误当成新账号；valid_account_name 也拒绝 _ 开头，双重保险。运行完立即删除。"""
    tmp_user = dict(user)
    tmp_user["photo"] = photo_override
    proxy = str(user.get("httpProxy", "") or "")
    tmp = ACCOUNTS_DIR / ("_tmp-%s-%s.yml" % (name, uuid.uuid4().hex[:8]))
    tmp.write_text(dump_account(tmp_user, proxy), encoding="utf-8")
    return str(tmp)

def cleanup_stale_temp_accounts():
    """清理上次运行残留的临时配置（进程被强杀时 finally 不会执行）。
    只删 _tmp- 开头的 .yml，不碰真实账号。"""
    if not ACCOUNTS_DIR.is_dir():
        return 0
    n = 0
    for f in os.listdir(ACCOUNTS_DIR):
        if f.startswith("_tmp-") and f.endswith(".yml"):
            try:
                (ACCOUNTS_DIR / f).unlink()
                n += 1
            except OSError:
                pass
    if n:
        log.info("清理残留临时配置 %d 个", n)
    return n

def mask_username(name: str) -> str:
    if not name:
        return ""
    if len(name) <= 4:
        return name[0] + "***"
    return name[:4] + "***"

# ---------------- 照片池 ----------------
def list_pool_photos() -> list:
    """列出照片池中的图片文件名（按名称排序）。"""
    if not PHOTO_POOL_DIR.is_dir():
        return []
    out = []
    for f in sorted(os.listdir(PHOTO_POOL_DIR)):
        p = PHOTO_POOL_DIR / f
        if p.is_file() and f.lower().endswith(PHOTO_EXTS):
            out.append(f)
    return out

def pick_random_pool_photo() -> Optional[str]:
    """从照片池随机取一张，返回引擎可用的相对路径（相对 jrxy-engine/ 根目录）。
    池为空时返回 None，调用方应回退到账号原有 photo。"""
    photos = list_pool_photos()
    if not photos:
        return None
    return "../web-server/data/photo_pool/%s" % random.choice(photos)

def resolve_account_photo(user: dict) -> str:
    """决定本次运行实际使用的照片路径。
    photoRandom=1 且照片池非空时随机取一张；否则用账号固定 photo。"""
    try:
        use_random = int(user.get("photoRandom", 0) or 0) == 1
    except (TypeError, ValueError):
        use_random = False
    if use_random:
        picked = pick_random_pool_photo()
        if picked:
            return picked
        log.warning("账号开启随机照片，但照片池为空，回退使用 %s", user.get("photo") or "(未设置)")
    return str(user.get("photo", "") or "")

# ---------------- 高德地图配置 ----------------
def load_amap_conf() -> dict:
    """读取高德配置。key/securityJsCode 留空表示未启用地图选点。
    也支持用环境变量 AMAP_KEY / AMAP_SECURITY_CODE / AMAP_WEB_KEY 覆盖（便于容器部署）。
    结果按文件 mtime 缓存，避免 /api/status 高频调用反复读盘。"""
    global _amap_cache
    try:
        mtime = AMAP_CONF_PATH.stat().st_mtime if AMAP_CONF_PATH.exists() else 0
    except OSError:
        mtime = 0
    cache = _amap_cache
    if cache and cache.get("_mtime") == mtime:
        return dict(cache["conf"])
    conf = {"key": "", "securityJsCode": "", "webKey": ""}
    if mtime:
        try:
            d = json.loads(AMAP_CONF_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for k in conf:
                    conf[k] = str(d.get(k, "") or "")
        except Exception as e:
            log.warning("读取 amap.json 失败: %s", e)
    env_key = os.getenv("AMAP_KEY", "").strip()
    env_sec = os.getenv("AMAP_SECURITY_CODE", "").strip()
    env_web = os.getenv("AMAP_WEB_KEY", "").strip()
    if env_key:
        conf["key"] = env_key
    if env_sec:
        conf["securityJsCode"] = env_sec
    if env_web:
        conf["webKey"] = env_web
    _amap_cache = {"_mtime": mtime, "conf": dict(conf)}
    return conf

def save_amap_conf(conf: dict):
    global _amap_cache
    data = {k: str(conf.get(k, "") or "") for k in ("key", "securityJsCode", "webKey")}
    tmp = AMAP_CONF_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, AMAP_CONF_PATH)
    _amap_cache = None      # 让下次读取重新加载

def normalize_optional_num(v, default=None):
    """数字字段：空串/None → default；否则 float。"""
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="经度/纬度必须是数字")

def account_summary(name: str) -> dict:
    user, dev = read_account(name)
    return {
        "name": name,
        "school": user.get("schoolName", ""),
        "username": user.get("username", ""),
        "type": user.get("type", 2),
        "lon": user.get("lon"),
        "lat": user.get("lat"),
        "address": user.get("address", ""),
        "photo": user.get("photo", ""),
        "photo_random": int(user.get("photoRandom", 0) or 0) == 1,
        "password_set": bool(user.get("password")),
        "device_id": dev or "（未设置，首次运行自动生成）",
        # 按账号独立定时
        "schedule_enabled": int(user.get("scheduleEnabled", 0) or 0) == 1,
        "schedule_time": str(user.get("scheduleTime", "") or ""),
    }

# ---------------- 引擎执行 ----------------
def run_cmd(cmd: list, timeout: int = 180, extra_env: Optional[dict] = None) -> str:
    env = dict(os.environ)
    env["JRXY_ACCOUNTS_DIR"] = str(ACCOUNTS_DIR)
    # 子进程 stdout 是管道：Windows 默认按 GBK 输出，与下面的 utf-8 解码不一致会把中文变乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(cmd, cwd=str(ENGINE_DIR), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return "执行超时（%ss），引擎可能卡在登录/网络请求上。" % timeout
    except Exception as e:
        return "执行异常: %s" % e
    out = (r.stdout or "").strip()
    if r.stderr and r.stderr.strip():
        out += "\n[stderr]\n" + r.stderr.strip()
    return out or "(无输出)"

RESULT_LABELS = {
    "success": "签到成功",
    "already": "今日已签到",
    "no_task": "暂无待签任务",
    "dry": "探测通过",
    "fail": "签到失败",
    "error": "执行异常",
}

def classify(out: str) -> str:
    """优先读引擎输出的 ###RESULT### 行；没有该行（超时/旧引擎）时回退关键词判断。"""
    res = parse_marker(out, "###RESULT###")
    if isinstance(res, dict) and res.get("status") in RESULT_LABELS:
        label = RESULT_LABELS[res["status"]]
        msg = str(res.get("msg", "")).strip()
        if res["status"] in ("already", "fail", "error") and msg:
            return msg[:100] if msg.startswith(label) else "%s：%s" % (label, msg[:80])
        return label
    if "执行超时" in out or "引擎虚拟环境不存在" in out:
        return "执行异常"
    if "今日已签到" in out:
        return "今日已签到"
    if "没有未签到的任务" in out:
        return "暂无待签任务"
    if "SUCCESS" in out or "签到成功" in out:
        return "签到成功"
    if "已签到" in out or "重复提交" in out:
        return "今日已签到（重复提交）"
    if "登录失败" in out or "Traceback" in out or "Exception" in out:
        return "执行异常"
    return "执行完成（见详情）"

def notify_head(result: str) -> str:
    """推送标题用的状态词：取结果标签冒号前的部分。"""
    return (result or "未知").split("：")[0].split("（")[0].strip()

def send_notify(name: str, title: str, msg: str) -> str:
    """按账号 notifyOption 推送签到结果，返回推送结果说明（写进运行日志，便于排查）。
    method=2 走 Server酱/任意 Webhook（rcvOption 填 SendKey 或完整地址）。"""
    try:
        user, _ = read_account(name)
    except HTTPException as e:
        return "推送跳过：%s" % e.detail
    no = user.get("notifyOption") or {}
    try:
        method = int(no.get("method", 0) or 0)
    except (TypeError, ValueError):
        method = 0
    rcv = str(no.get("rcvOption", "")).strip()
    if method != 2 or not rcv:
        return "推送未启用（账号未配置 Server酱）"
    # 只填 SendKey 时自动拼地址：sctp 开头为 Server酱³，SCT 开头为 Server酱 Turbo
    m = re.match(r"^sctp(\d+)t", rcv, re.I)
    if m:
        rcv = "https://%s.push.ft07.com/send/%s.send" % (m.group(1), rcv)
    elif rcv.startswith("SCT"):
        rcv = "https://sctapi.ftqq.com/%s.send" % rcv
    data = urllib.parse.urlencode({"title": title[:32], "desp": msg}).encode("utf-8")
    req = urllib.request.Request(
        rcv, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return "推送失败：HTTP %s %s" % (e.code, body[:200])
    except Exception as e:
        return "推送失败：%s" % e
    try:
        j = json.loads(body)
    except ValueError:
        return "推送已发送（非 JSON 响应）"
    code = j.get("code", 0)
    if code in (0, "0"):
        return "推送成功（Server酱已接收）"
    return "推送失败：code=%s %s" % (code, str(j.get("message") or j.get("info") or "")[:200])

def run_account(name: str, dry: bool) -> str:
    if not ENGINE_PY.exists():
        return "引擎虚拟环境不存在: %s\n请先执行 deploy/install_windows.bat 或 deploy/install_linux.sh" % ENGINE_PY
    yml, _ = account_paths(name)
    try:
        did = ensure_device_id(name)
    except HTTPException as e:
        return str(e.detail)
    # 开启“照片池随机”时，把随机选中的照片写进临时配置副本，避免污染 accounts/*.yml
    conf_path = str(yml)
    try:
        user, _ = read_account(name)
        if int(user.get("photoRandom", 0) or 0) == 1:
            picked = resolve_account_photo(user)
            if picked and picked != str(user.get("photo", "") or ""):
                tmp_conf = write_temp_account(name, user, picked)
                conf_path = tmp_conf
                log.info("账号 %s 使用照片池随机照片: %s", name, picked)
    except Exception as e:
        log.warning("准备随机照片失败，回退原配置: %s", e)
    cmd = [str(ENGINE_PY), "-W", "ignore", str(SUBMIT_SCRIPT),
           "--dry" if dry else "--submit",
           "--variant", "fkey_cat", "--protocol", "ios",
           "--config", conf_path, "--device-id", did]
    log.info("run_account %s dry=%s", name, dry)
    with _run_lock:
        try:
            return run_cmd(cmd, timeout=RUN_TIMEOUT)
        finally:
            if conf_path != str(yml):
                try:
                    os.remove(conf_path)
                except OSError:
                    pass

def is_exec_error(result: str) -> bool:
    return notify_head(result) == "执行异常"

def run_and_report(name: str, dry: bool, trigger: str, will_retry: bool = False) -> tuple:
    """执行单账号 → 判定结果 → 推送 → 写日志。返回 (result, out)。
    手动、批量、定时三条路径都走这里，保证结果判定和通知一致。
    will_retry=True 表示本次若执行异常还会重试，此时不推送，只推送最终结果。"""
    action = "探测" if dry else "签到"
    started = now()
    out = run_account(name, dry)
    result = classify(out)
    if dry and not NOTIFY_ON_DRY:
        push = "推送跳过（探测默认不推送，可设 NOTIFY_ON_DRY=1）"
    elif will_retry and is_exec_error(result):
        push = "推送跳过（执行异常，%g 分钟后重试）" % RETRY_DELAY_MINUTES
    else:
        head = notify_head(result)
        msg = "\n\n".join([
            "**%s**" % result,
            "- 账号：%s" % name,
            "- 触发：%s%s" % (trigger, action),
            "- 开始：%s" % started.strftime("%Y-%m-%d %H:%M:%S"),
            "- 结束：%s" % now().strftime("%Y-%m-%d %H:%M:%S"),
            "```\n%s\n```" % (out.strip()[-1200:] or "(无输出)"),
        ])
        push = send_notify(name, "【%s】%s" % (head, name), msg)
    log.info("[%s%s] %s -> %s | %s", trigger, action, name, result, push)
    save_log("%s%s[%s]" % (trigger, action, name), result, "%s\n\n[%s]" % (out, push))
    return result, out

def summarize(results: dict) -> tuple:
    """results: {账号名: 结果}，返回 (汇总, 详情)。"""
    counts = {}
    for r in results.values():
        head = notify_head(r)
        counts[head] = counts.get(head, 0) + 1
    summary = " / ".join("%s %d" % kv for kv in counts.items()) or "没有可用账号"
    return summary, "\n".join("%s：%s" % kv for kv in results.items())

def run_all(dry: bool, trigger: str, skip=(), on_done=None) -> tuple:
    """逐个账号串行执行（间隔 ACCOUNT_INTERVAL 秒错峰），返回 (汇总, 详情)。
    skip 中的账号不执行；on_done(账号名, 结果) 在每个账号完成后立即回调（用于落盘进度）。"""
    names = list_accounts()
    if not names:
        return "没有可用账号", "未在 %s 找到账号配置" % ACCOUNTS_DIR
    results = {}
    for nm in names:
        if nm in skip:
            continue
        if results:
            time.sleep(ACCOUNT_INTERVAL)
        try:
            result, _ = run_and_report(nm, dry, trigger)
        except Exception as e:
            result = "执行异常：%s" % e
            log.exception("run_all %s failed", nm)
        results[nm] = result
        if on_done:
            on_done(nm, result)
    return summarize(results)

def parse_marker(out: str, marker: str):
    for line in out.splitlines():
        if line.startswith(marker):
            try:
                return json.loads(line[len(marker):].strip())
            except Exception:
                return None
    return None

# ---------------- 定时（线程版，触发时批量跑全部账号） ----------------
# schedule.json：
#   last_slot  最近一个“全部账号都已执行完”的计划时间点（"YYYY-MM-DD HH:MM"），每个点只执行一次
#   progress   正在执行的时间点及已完成账号 {"slot":..., "started":..., "results": {账号: 结果}}；
#              执行中途重启后，补签窗口内只补跑 results 里没有的账号
_SCHED_KEYS = ("enabled", "time", "last_slot", "last_run", "last_result", "progress",
               "acct_slots")

def load_schedule():
    st = {"enabled": DEFAULT_AUTO_SIGN, "time": DEFAULT_SIGN_TIME, "next": None,
          "last_slot": None, "last_run": None, "last_result": None, "progress": None,
          "acct_slots": {}}
    if SCHED_PATH.exists():
        try:
            d = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
            st["enabled"] = bool(d.get("enabled", st["enabled"]))
            st["time"] = str(d.get("time", st["time"]))
            for k in ("last_slot", "last_run", "last_result", "progress", "acct_slots"):
                st[k] = d.get(k)
        except Exception:
            pass
    if not isinstance(st["progress"], dict) or not isinstance(st["progress"].get("results"), dict):
        st["progress"] = None
    if not isinstance(st.get("acct_slots"), dict):
        st["acct_slots"] = {}
    return st

_sched_file_lock = threading.Lock()

def save_schedule():
    with _sched_file_lock:
        d = {k: scheduler_state.get(k) for k in _SCHED_KEYS}
        tmp = SCHED_PATH.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, SCHED_PATH)   # 原子替换：写到一半断电也不会留下损坏的 schedule.json
        except Exception as e:
            log.warning("save_schedule failed: %s", e)

scheduler_state = load_schedule()
_sched_wake = threading.Event()

def _parse_hm(s: str):
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except Exception:
        raise ValueError("时间格式应为 HH:MM（如 22:05）")

SLOT_FMT = "%Y-%m-%d %H:%M"

def _slot_today(time_str: str, ref: datetime) -> datetime:
    h, m = _parse_hm(time_str)
    return ref.replace(hour=h, minute=m, second=0, microsecond=0)

def _mark_past_slot_done():
    """把“最近一个已过去的时间点”记为已执行：保存/启用定时不会立刻补跑，下次执行一定在未来。"""
    t = now()
    slot = _slot_today(scheduler_state["time"], t)
    if slot > t:
        slot -= timedelta(days=1)
    prog = scheduler_state.get("progress")
    if prog and prog.get("slot") == slot.strftime(SLOT_FMT):
        return   # 该时间点还有账号没跑完，保留给补签
    scheduler_state["last_slot"] = slot.strftime(SLOT_FMT)

def _pending_slot(time_str: str):
    """返回下一个待执行的时间点：今天的点已过但未执行且仍在补签窗口内则返回它（立即执行），
    否则返回未来最近的点。"""
    t = now()
    today = _slot_today(time_str, t)
    done = scheduler_state.get("last_slot")
    if today <= t:
        if done != today.strftime(SLOT_FMT) and t - today <= timedelta(minutes=CATCHUP_MINUTES):
            return today
        return today + timedelta(days=1)
    return today

def _wait_until(t: datetime):
    """睡到 t（按 now() 计时），分段睡，便于测试与时钟跳变。"""
    while True:
        left = (t - now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))

# ---------------- 按账号独立定时 ----------------
def per_account_schedules() -> dict:
    """返回 {账号名: "HH:MM"}，仅含开启了“独立定时”且时间合法的账号。
    这些账号不再跟随全局定时，改用自己的时间点。"""
    out = {}
    for nm in list_accounts():
        try:
            user, _ = read_account(nm)
        except Exception:
            continue
        try:
            enabled = int(user.get("scheduleEnabled", 0) or 0) == 1
        except (TypeError, ValueError):
            enabled = False
        tm = str(user.get("scheduleTime", "") or "").strip()
        if not enabled or not tm:
            continue
        try:
            _parse_hm(tm)
        except ValueError:
            continue
        out[nm] = tm
    return out

def _acct_slot_state() -> dict:
    """按账号的已执行记录：{账号名: "YYYY-MM-DD HH:MM"}，存在 schedule.json 里。"""
    st = scheduler_state.get("acct_slots")
    return st if isinstance(st, dict) else {}

def _mark_acct_slot_done(names_times: dict):
    """把“最近一个已过去的时间点”按账号记为已执行，避免保存设置后立即补跑。"""
    t = now()
    slots = scheduler_state.setdefault("acct_slots", {})
    for nm, tm in names_times.items():
        try:
            slot = _slot_today(tm, t)
        except ValueError:
            continue
        if slot > t:
            slot -= timedelta(days=1)
        slots[nm] = slot.strftime(SLOT_FMT)

def _next_acct_slot(nm: str, tm: str):
    """返回该账号下一个待执行的时间点（当天已过且仍在补签窗口内 → 立即执行）。"""
    t = now()
    today = _slot_today(tm, t)
    done = _acct_slot_state().get(nm)
    if today <= t:
        if done != today.strftime(SLOT_FMT) and t - today <= timedelta(minutes=CATCHUP_MINUTES):
            return today
        return today + timedelta(days=1)
    return today

def _fire_one_account(nm: str, slot: datetime, tm: str):
    """执行单个账号的独立定时（含重试）。"""
    slot_s = slot.strftime(SLOT_FMT)
    started = now()
    delay = int((started - slot).total_seconds())
    trigger = "独立定时" if delay < 120 else "独立定时补签"
    log.info("账号 %s %s触发：计划 %s，实际 %s（延迟 %ss）",
             nm, trigger, slot_s, started.strftime("%H:%M:%S"), delay)
    max_attempts = 1 + max(0, MAX_RETRIES)
    result = None
    attempts = 0
    while attempts < max_attempts:
        attempts += 1
        will_retry = attempts < max_attempts
        try:
            result, _ = run_and_report(nm, False, "%s(%s)" % (trigger, tm), will_retry=will_retry)
        except Exception as e:
            result = "执行异常：%s" % e
            log.exception("独立定时 %s 执行出错", nm)
        if not is_exec_error(result) or not will_retry:
            break
        log.info("账号 %s 执行异常，%s 分钟后重试（第 %d/%d 次）",
                 nm, RETRY_DELAY_MINUTES, attempts, max_attempts)
        _wait_until(now() + timedelta(minutes=RETRY_DELAY_MINUTES))
    # 记录该账号该时间点已执行
    scheduler_state.setdefault("acct_slots", {})[nm] = slot_s
    scheduler_state["last_run"] = started.strftime("%Y-%m-%d %H:%M:%S")
    save_schedule()
    save_log("独立定时汇总[%s]" % nm, result or "无结果",
             "账号 %s 的独立定时\n计划时间 %s（UTC%+g）\n开始 %s\n结束 %s\n共执行 %d 次" % (
                 nm, slot_s, TZ_OFFSET, started.strftime("%Y-%m-%d %H:%M:%S"),
                 now().strftime("%Y-%m-%d %H:%M:%S"), attempts))
    return result

def _fire(slot: datetime):
    """执行一个定时点：先串行跑全部账号；“执行异常”的账号隔 RETRY_DELAY_MINUTES 分钟
    再串行重试，最多 MAX_RETRIES 次，成功/已签/失败即停止。
    progress 记录每个账号的结果与已执行次数，每跑完一个账号就落盘，中途重启可接着跑。"""
    slot_s = slot.strftime(SLOT_FMT)
    started = now()
    delay = int((started - slot).total_seconds())
    prog = scheduler_state.get("progress")
    if not prog or prog.get("slot") != slot_s:
        prog = {"slot": slot_s, "started": started.strftime("%Y-%m-%d %H:%M:%S"), "results": {}}
    prog.setdefault("attempts", {nm: 1 for nm in prog["results"]})
    resumed = bool(prog["results"])
    base_trigger = "定时续签" if resumed else ("定时" if delay < 120 else "定时补签")
    scheduler_state["progress"] = prog
    save_schedule()
    log.info("%s触发：计划 %s，实际 %s（延迟 %ss）%s", base_trigger, slot_s,
             started.strftime("%H:%M:%S"), delay,
             "，已执行 %s，只跑剩余/待重试账号" % "、".join(prog["results"]) if resumed else "")
    max_attempts = 1 + max(0, MAX_RETRIES)

    def run_one(nm, trigger):
        n = prog["attempts"].get(nm, 0) + 1
        try:
            result, _ = run_and_report(nm, False, trigger, will_retry=n < max_attempts)
        except Exception as e:
            result = "执行异常：%s" % e
            log.exception("定时 %s 执行出错", nm)
        prog["attempts"][nm] = n
        prog["results"][nm] = result
        save_schedule()   # 每跑完一个账号就落盘，中途重启不会重签已完成的账号

    try:
        names = list_accounts()
        # 开启了“独立定时”的账号由各自的定时器负责，全局批量时跳过，避免重复签到
        per = per_account_schedules()
        names = [n for n in names if n not in per]
        if per:
            log.info("以下账号已启用独立定时，本次全局定时跳过：%s", "、".join(per))
        first = True
        # 第一轮：还没执行过的账号
        for nm in [n for n in names if n not in prog["results"]]:
            if not first:
                time.sleep(ACCOUNT_INTERVAL)
            first = False
            run_one(nm, base_trigger)
        # 重试轮：只针对执行异常且还有剩余次数的账号
        while True:
            todo = [n for n in names if n in prog["results"]
                    and is_exec_error(prog["results"][n])
                    and prog["attempts"].get(n, 1) < max_attempts]
            if not todo:
                break
            if not prog.get("retry_at"):
                prog["retry_at"] = (now() + timedelta(minutes=RETRY_DELAY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
                save_schedule()
            log.info("执行异常账号 %s 将于 %s 重试", "、".join(todo), prog["retry_at"])
            _wait_until(datetime.strptime(prog["retry_at"], "%Y-%m-%d %H:%M:%S"))
            for i, nm in enumerate(todo):
                if i:
                    time.sleep(ACCOUNT_INTERVAL)
                run_one(nm, "定时重试%d" % prog["attempts"].get(nm, 1))
            prog["retry_at"] = None
            save_schedule()
    finally:
        # 无论成功与否都把该时间点标记为已执行，避免异常时同一时间点被反复触发
        scheduler_state["last_slot"] = slot_s
        scheduler_state["progress"] = None
        summary, detail = summarize(prog["results"])
        scheduler_state["last_run"] = prog["started"]
        scheduler_state["last_result"] = summary
        save_schedule()
    tries = "\n".join("%s：共执行 %d 次" % (nm, n) for nm, n in prog["attempts"].items() if n > 1)
    save_log("%s汇总" % base_trigger, summary,
             "计划时间 %s（UTC%+g）\n首次开始 %s\n本次开始 %s（延迟 %ss）\n结束 %s\n\n%s%s" % (
                 slot_s, TZ_OFFSET, prog["started"],
                 started.strftime("%Y-%m-%d %H:%M:%S"), delay, now().strftime("%Y-%m-%d %H:%M:%S"),
                 detail, ("\n\n重试情况：\n" + tries) if tries else ""))

def scheduler_loop():
    """主循环：全局定时 + 各账号的独立定时。
    每轮都重新计算“最近的下一个待执行时间点”，取全局与全部独立定时中最早的；
    睡最多 30 秒后按墙上时间重算，设置变更能及时生效。"""
    while True:
        st = scheduler_state
        try:
            # 1) 全局定时（跳过已启用独立定时的账号）
            cand = []            # [(时间点, 类型, 账号名或None, 时间字符串)]
            if st["enabled"]:
                gslot = _pending_slot(st["time"])
                cand.append((gslot, "global", None, st["time"]))

            # 2) 各账号独立定时（不受全局 enabled 开关影响，账号自己说了算）
            per = per_account_schedules()
            for nm, tm in per.items():
                try:
                    cand.append((_next_acct_slot(nm, tm), "acct", nm, tm))
                except ValueError:
                    continue

            # 清理已不存在/已关闭独立定时的账号记录
            slots = _acct_slot_state()
            for gone in [n for n in slots if n not in per]:
                slots.pop(gone, None)

            if not cand:
                st["next"] = None
                _sched_wake.wait(timeout=30)
                _sched_wake.clear()
                continue

            cand.sort(key=lambda x: x[0])
            nxt, kind, nm, tm = cand[0]
            st["next"] = nxt.strftime("%Y-%m-%d %H:%M:%S")
            st["next_kind"] = "acct:%s@%s" % (nm, tm) if kind == "acct" else "global@%s" % tm
            wait = (nxt - now()).total_seconds()
            if wait <= 0:
                if kind == "acct":
                    _fire_one_account(nm, nxt, tm)
                else:
                    _fire(nxt)
                continue
            # 每次最多睡 30 秒再按墙上时间重算：设置变更、系统对时跳变都能及时生效
            _sched_wake.wait(timeout=min(wait, 30))
            _sched_wake.clear()
        except Exception:
            log.exception("scheduler loop error")
            time.sleep(30)

def _start_scheduler():
    if not scheduler_state.get("last_slot") and not scheduler_state.get("progress"):
        # 首次启动/从旧版升级：没有执行记录时不补跑已过去的时间点
        try:
            _mark_past_slot_done()
            save_schedule()
        except ValueError:
            pass
    # 独立定时的账号同样不补跑“已过去的时间点”
    try:
        _mark_acct_slot_done(per_account_schedules())
        save_schedule()
    except Exception:
        log.exception("初始化账号独立定时失败")
    log.info("内置定时：enabled=%s time=%s 时区=UTC%+g 当前=%s token=%s",
             scheduler_state["enabled"], scheduler_state["time"], TZ_OFFSET,
             now().strftime("%Y-%m-%d %H:%M:%S"), "已启用" if WEB_TOKEN else "未设置")
    per = per_account_schedules()
    if per:
        log.info("账号独立定时：%s", "、".join("%s@%s" % kv for kv in per.items()))
    threading.Thread(target=scheduler_loop, daemon=True, name="jrxy-scheduler").start()

# ---------------- FastAPI ----------------
def require_token(x_web_token: Optional[str] = Header(default=None)):
    if WEB_TOKEN and not hmac.compare_digest((x_web_token or "").encode(), WEB_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="未授权：缺少或错误的 X-Web-Token")

app = FastAPI(title="今日校园自动签到 · 多账号管理台", docs_url="/api/docs", redoc_url=None)

@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(INDEX_HTML)

@app.get("/school_list.js")
def school_list_js():
    return FileResponse(STATIC_DIR / "school_list.js", media_type="application/javascript")

@app.get("/api/status")
def api_status(_t: None = Depends(require_token)):
    return {
        "platform": "Windows" if IS_WIN else "Linux",
        "engine_ready": ENGINE_PY.exists(),
        "accounts_dir": str(ACCOUNTS_DIR),
        "account_count": len(list_accounts()),
        "scheduler": {
            "enabled": scheduler_state["enabled"],
            "time": scheduler_state["time"],
            "next": scheduler_state["next"],
            "next_kind": scheduler_state.get("next_kind"),
            "last_run": scheduler_state.get("last_run"),
            "last_result": scheduler_state.get("last_result"),
        },
        "per_account_schedules": per_account_schedules(),
        "photo_pool": {"count": len(list_pool_photos()), "dir": str(PHOTO_POOL_DIR)},
        "amap": {"enabled": bool(load_amap_conf()["key"])},
        "server_time": now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": "UTC%+g" % TZ_OFFSET,
        "system_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------------- 账号管理 ----------------
@app.get("/api/accounts")
def api_accounts(_t: None = Depends(require_token)):
    names = list_accounts()
    return {"accounts": [account_summary(n) for n in names]}

@app.get("/api/accounts/{name}")
def api_account_get(name: str, _t: None = Depends(require_token)):
    user, dev = read_account(name)
    no = user.get("notifyOption") or {}
    return {
        "name": name,
        "schoolName": user.get("schoolName", ""),
        "type": user.get("type", 2),
        "httpProxy": user.get("httpProxy", ""),
        "lon": user.get("lon"),
        "lat": user.get("lat"),
        "address": user.get("address", ""),
        "abnormalReason": user.get("abnormalReason", ""),
        "photo": user.get("photo", ""),
        "photoRandom": int(user.get("photoRandom", 0) or 0),
        "scheduleEnabled": int(user.get("scheduleEnabled", 0) or 0),
        "scheduleTime": str(user.get("scheduleTime", "") or ""),
        "checkTitle": user.get("checkTitle", 0),
        "username": user.get("username", ""),
        "username_set": bool(user.get("username")),
        "password_set": bool(user.get("password")),
        "device_id": dev,
        "notify_method": int(no.get("method", 0) or 0),
        "notify_rcv": str(no.get("rcvOption", "")).strip(),
    }

def build_user_from_body(body: dict, existing: dict = None) -> dict:
    """构造/合并 user 字典；existing 为空表示新建（必填 schoolName/username/password）。"""
    cur = dict(existing or {})
    schoolName = str(body.get("schoolName", "")).strip() or cur.get("schoolName", "")
    username = str(body.get("username", "")).strip() or cur.get("username", "")
    password = str(body.get("password", "")) or cur.get("password", "")
    if not existing and not (schoolName and username and password):
        raise HTTPException(status_code=400,
                            detail="新建账号必须填写：学校名称 / 学号 / 密码")
    try:
        typ = int(body.get("type", cur.get("type", 2)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="任务类型必须是 0~4 的整数")
    if typ not in (0, 1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="任务类型仅支持 0收集/1签到/2查寝/3政工/4日志")
    lon = normalize_optional_num(body.get("lon"), cur.get("lon"))
    lat = normalize_optional_num(body.get("lat"), cur.get("lat"))
    if lon is not None and not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="经度范围 -180 ~ 180")
    if lat is not None and not (-90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="纬度范围 -90 ~ 90")
    try:
        checkTitle = int(body.get("checkTitle", cur.get("checkTitle", 0)) or 0)
    except (TypeError, ValueError):
        checkTitle = 0
    # 消息推送：method 0=禁用 2=Server酱/任意Webhook（rcvOption 填推送地址）
    cur_no = cur.get("notifyOption") or {}
    if not isinstance(cur_no, dict):
        cur_no = {}
    try:
        n_method = int(body.get("notifyMethod", cur_no.get("method", 0)) or 0)
    except (TypeError, ValueError):
        n_method = 0
    if n_method not in (0, 1, 2):
        n_method = 0
    notify = {
        "method": n_method,
        "rcvOption": str(body.get("notifyRcv", cur_no.get("rcvOption", ""))).strip(),
    }
    # 照片池随机：1=每次运行从照片池随机取一张
    raw_pr = body.get("photoRandom", cur.get("photoRandom", 0))
    try:
        photoRandom = 1 if int(raw_pr or 0) == 1 else 0
    except (TypeError, ValueError):
        photoRandom = 1 if raw_pr is True else 0
    # 按账号独立定时
    raw_se = body.get("scheduleEnabled", cur.get("scheduleEnabled", 0))
    try:
        scheduleEnabled = 1 if int(raw_se or 0) == 1 else 0
    except (TypeError, ValueError):
        scheduleEnabled = 1 if raw_se is True else 0
    scheduleTime = str(body.get("scheduleTime", cur.get("scheduleTime", "")) or "").strip()
    if scheduleTime:
        _parse_hm(scheduleTime)      # 非法时间直接抛 400
    user = {
        "type": typ,
        "schoolName": schoolName,
        "username": username,
        "password": password,
        "address": str(body.get("address", cur.get("address", ""))).strip(),
        "lon": lon,
        "lat": lat,
        "abnormalReason": str(body.get("abnormalReason", cur.get("abnormalReason", ""))).strip(),
        "photo": str(body.get("photo", cur.get("photo", ""))).strip(),
        "photoRandom": photoRandom,
        "scheduleEnabled": scheduleEnabled,
        "scheduleTime": scheduleTime,
        "checkTitle": checkTitle,
        "notifyOption": notify,
    }
    return user, str(body.get("httpProxy", "")).strip()

@app.post("/api/accounts")
def api_account_create(body: dict, _t: None = Depends(require_token)):
    name = str(body.get("name", "")).strip()
    if not valid_account_name(name):
        raise HTTPException(status_code=400,
                            detail="账号名需为 1~32 位中英文/数字/_- 且不能以 _ 开头")
    yml, did_file = account_paths(name)
    if yml.exists():
        raise HTTPException(status_code=400, detail=f"账号 {name} 已存在")
    user, proxy = build_user_from_body(body)
    write_account(name, user, proxy)
    dev = str(body.get("deviceId", "")).strip()
    if dev:
        did_file.write_text(dev, encoding="utf-8")
    save_log("账号管理", "账号已创建", "name=%s school=%s" % (name, user["schoolName"]))
    return {"ok": True, "account": account_summary(name),
            "note": ("已写入固定 deviceId" if dev else "deviceId 未设置，首次运行自动生成")}

@app.post("/api/accounts/{name}")
def api_account_update(name: str, body: dict, _t: None = Depends(require_token)):
    user, dev = read_account(name)
    new_user, proxy = build_user_from_body(body, existing=user)
    # 密码留空 = 保持原密码（build_user_from_body 已处理）；账号留空同理
    write_account(name, new_user, proxy)
    dev_new = str(body.get("deviceId", "")).strip()
    if dev_new:
        _, did_file = account_paths(name)
        did_file.write_text(dev_new, encoding="utf-8")
    save_log("账号管理", "账号已更新", "name=%s school=%s" % (name, new_user["schoolName"]))
    # 独立定时变更后立即让调度器重算，避免等最多 30 秒
    try:
        if int(new_user.get("scheduleEnabled", 0) or 0) == 1:
            tm = str(new_user.get("scheduleTime", "") or "")
            if tm:
                # 把“已过去的时间点”标记为已执行，保存设置不会立刻补跑
                _mark_acct_slot_done({name: tm})
                save_schedule()
        _sched_wake.set()
    except Exception:
        log.exception("唤醒调度器失败")
    return {"ok": True, "account": account_summary(name)}

@app.delete("/api/accounts/{name}")
def api_account_delete(name: str, _t: None = Depends(require_token)):
    read_account(name)   # 校验存在
    yml, did_file = account_paths(name)
    yml.unlink(missing_ok=True)
    # deviceId 文件保留（若账号重新接入，可沿用原绑定；文件不随 yml 被批量脚本扫描）
    save_log("账号管理", "账号已删除", "name=%s（deviceId 文件保留以防误删绑定）" % name)
    _sched_wake.set()   # 让调度器重算（该账号的独立定时记录会被清掉）
    return {"ok": True, "note": "已删除账号配置；deviceId 文件已保留（重新接入可沿用原绑定）"}

# ---------------- 账号操作：探测/签到/登录测试/签到范围/照片 ----------------
@app.post("/api/accounts/{name}/run")
def api_account_run(name: str, body: dict, _t: None = Depends(require_token)):
    mode = str(body.get("mode", "dry"))
    if mode not in ("dry", "submit"):
        raise HTTPException(status_code=400, detail="mode 仅支持 dry / submit")
    read_account(name)
    result, out = run_and_report(name, dry=(mode == "dry"), trigger="手动")
    return {"ok": True, "result": result, "detail": out[-4000:]}

@app.post("/api/accounts/{name}/test-login")
def api_account_test_login(name: str, _t: None = Depends(require_token)):
    read_account(name)
    if not ENGINE_PY.exists():
        raise HTTPException(status_code=500, detail="引擎虚拟环境不存在")
    if not TESTLOGIN_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="引擎脚本不存在")
    yml, _ = account_paths(name)
    cmd = [str(ENGINE_PY), "-W", "ignore", str(TESTLOGIN_SCRIPT), "--config", str(yml)]
    out = run_cmd(cmd, timeout=60)
    payload = parse_marker(out, "###LOGIN###")
    if not payload:
        save_log(f"账号[{name}] 登录测试", "测试失败", (out or "")[-300:])
        raise HTTPException(status_code=502, detail="无法解析结果: %s" % (out[-300:] or "无输出"))
    if payload.get("ok"):
        save_log(f"账号[{name}] 登录测试", "登录成功",
                 "host=%s cookies=%s" % (payload.get("host"), ",".join(payload.get("cookies") or [])))
    else:
        save_log(f"账号[{name}] 登录测试", "登录失败", str(payload.get("error"))[:200])
    return {"ok": bool(payload.get("ok")), "host": payload.get("host", ""),
            "cookies": payload.get("cookies", []), "error": payload.get("error")}

@app.post("/api/accounts/{name}/sign-range")
def api_account_sign_range(name: str, _t: None = Depends(require_token)):
    user, _ = read_account(name)
    if not ENGINE_PY.exists():
        raise HTTPException(status_code=500, detail="引擎虚拟环境不存在")
    if not RANGE_SCRIPT.exists():
        raise HTTPException(status_code=500, detail="引擎脚本不存在")
    yml, _ = account_paths(name)
    cmd = [str(ENGINE_PY), "-W", "ignore", str(RANGE_SCRIPT), "--config", str(yml)]
    out = run_cmd(cmd, timeout=120)
    payload = parse_marker(out, "###RANGE###")
    if not payload or not payload.get("ok"):
        err = (payload or {}).get("error") or (out[-300:] or "无输出")
        save_log(f"账号[{name}] 范围获取", "获取失败", str(err)[:200])
        raise HTTPException(status_code=502, detail="获取失败: %s" % err)
    lon, lat = float(payload["lon"]), float(payload["lat"])
    user["lon"], user["lat"] = lon, lat
    user["address"] = payload.get("address") or user.get("address", "")
    write_account(name, user)
    detail = "center=(%.8f, %.8f) radius=%sm %s 窗口 %s~%s" % (
        lon, lat, payload.get("radius"), payload.get("taskName", ""),
        payload.get("window_begin", ""), payload.get("window_end", ""))
    save_log(f"账号[{name}] 范围获取", "签到范围已获取并应用", detail)
    return {"ok": True, "range": payload, "account": account_summary(name)}

@app.post("/api/accounts/{name}/photo")
def api_account_photo(name: str, body: dict, _t: None = Depends(require_token)):
    user, _ = read_account(name)
    fname = os.path.basename(str(body.get("name", "")).strip())
    data = str(body.get("data", ""))
    if not fname or not data:
        raise HTTPException(status_code=400, detail="缺少文件名或图片数据")
    if not fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp/gif 图片")
    try:
        raw = base64.b64decode(data.split(",")[-1])
    except Exception:
        raise HTTPException(status_code=400, detail="图片数据解码失败")
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    adir = ACCOUNTS_DIR / name
    adir.mkdir(parents=True, exist_ok=True)
    target = adir / fname
    target.write_bytes(raw)
    # 引擎按 jrxy-engine/ 根目录解析 photo；accounts 在项目根，故用 ../accounts/<name>/<file>
    user["photo"] = "../accounts/%s/%s" % (name, fname)
    write_account(name, user)
    save_log(f"账号[{name}] 照片", "签到照片已上传", "file=%s size=%dB" % (fname, len(raw)))
    return {"ok": True, "photo": user["photo"], "size": len(raw)}

# ---------------- 照片池 ----------------
@app.get("/api/photo-pool")
def api_pool_list(_t: None = Depends(require_token)):
    photos = list_pool_photos()
    items = []
    for f in photos:
        try:
            size = (PHOTO_POOL_DIR / f).stat().st_size
        except OSError:
            size = 0
        items.append({"name": f, "size": size, "url": "/api/photo-pool/file/" + urllib.parse.quote(f)})
    return {"ok": True, "dir": str(PHOTO_POOL_DIR), "count": len(items), "photos": items}

@app.get("/api/photo-pool/file/{fname}")
def api_pool_file(fname: str, _t: None = Depends(require_token)):
    """预览照片池中的单张图片。"""
    safe = os.path.basename(fname)
    p = PHOTO_POOL_DIR / safe
    if not safe or not p.is_file() or not safe.lower().endswith(PHOTO_EXTS):
        raise HTTPException(status_code=404, detail="照片不存在")
    return FileResponse(p)

@app.post("/api/photo-pool")
def api_pool_upload(body: dict, _t: None = Depends(require_token)):
    """上传一张图片到照片池。"""
    fname = os.path.basename(str(body.get("name", "")).strip())
    data = str(body.get("data", ""))
    if not fname or not data:
        raise HTTPException(status_code=400, detail="缺少文件名或图片数据")
    if not fname.lower().endswith(PHOTO_EXTS):
        raise HTTPException(status_code=400, detail="仅支持 jpg/png/webp/gif 图片")
    try:
        raw = base64.b64decode(data.split(",")[-1])
    except Exception:
        raise HTTPException(status_code=400, detail="图片数据解码失败")
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="图片不能超过 5MB")
    target = PHOTO_POOL_DIR / fname
    target.write_bytes(raw)
    save_log("照片池", "照片已加入池", "file=%s size=%dB 当前共 %d 张" % (fname, len(raw), len(list_pool_photos())))
    return {"ok": True, "name": fname, "size": len(raw), "count": len(list_pool_photos())}

@app.delete("/api/photo-pool/{fname}")
def api_pool_delete(fname: str, _t: None = Depends(require_token)):
    safe = os.path.basename(fname)
    p = PHOTO_POOL_DIR / safe
    if not safe or not p.is_file() or not safe.lower().endswith(PHOTO_EXTS):
        raise HTTPException(status_code=404, detail="照片不存在")
    p.unlink()
    save_log("照片池", "照片已移除", "file=%s 剩余 %d 张" % (safe, len(list_pool_photos())))
    return {"ok": True, "count": len(list_pool_photos())}

# ---------------- 高德地图选点配置 ----------------
@app.get("/api/amap-config")
def api_amap_get(_t: None = Depends(require_token)):
    """返回高德配置。key 回传给前端用于加载 JS API（JS Key 本就是公开给浏览器用的），
    webKey 只回传“是否已配置”，不回传明文，避免不必要的暴露。"""
    conf = load_amap_conf()
    return {
        "ok": True,
        "key": conf["key"],
        "securityJsCode": conf["securityJsCode"],
        "webKey_set": bool(conf["webKey"]),
        "enabled": bool(conf["key"]),
        "hint": "" if conf["key"] else "尚未配置高德 Key，地图选点不可用。请到 https://console.amap.com/dev/key 申请「Web端(JS API)」Key 后填入。",
    }

@app.post("/api/amap-config")
def api_amap_save(body: dict, _t: None = Depends(require_token)):
    key = str(body.get("key", "")).strip()
    sec = str(body.get("securityJsCode", "")).strip()
    web = body.get("webKey", None)
    cur = load_amap_conf()
    conf = {
        "key": key,
        "securityJsCode": sec,
        # webKey 不传表示保持原值（避免前端拿不到明文后误清空）
        "webKey": cur["webKey"] if web is None else str(web).strip(),
    }
    save_amap_conf(conf)
    save_log("高德地图", "地图配置已保存",
             "key=%s 安全密钥=%s Web服务Key=%s" % (
                 (key[:6] + "***") if key else "（空）",
                 "已设置" if sec else "未设置",
                 "已设置" if conf["webKey"] else "未设置"))
    return {"ok": True, "enabled": bool(key)}

@app.post("/api/amap/regeo")
def api_amap_regeo(body: dict, _t: None = Depends(require_token)):
    """逆地理编码：经纬度 → 地址文本。需要「Web服务」类型的 Key。
    若未配置 Web 服务 Key，返回 ok=False，前端会退化为只用坐标不填地址。"""
    conf = load_amap_conf()
    web_key = conf.get("webKey", "")
    if not web_key:
        return {"ok": False, "error": "未配置高德 Web 服务 Key，无法逆地理编码（坐标仍可使用）"}
    try:
        lon = float(body.get("lon"))
        lat = float(body.get("lat"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="经纬度必须是数字")
    # 高德逆地理编码：location 为 "经度,纬度"
    qs = urllib.parse.urlencode({
        "key": web_key, "location": "%.6f,%.6f" % (lon, lat), "extensions": "base",
    })
    url = "https://restapi.amap.com/v3/geocode/regeo?" + qs
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": "请求高德接口失败：%s" % e}
    if str(data.get("status")) != "1":
        return {"ok": False, "error": "高德返回错误：%s（info=%s）" % (
            data.get("info"), data.get("infocode"))}
    regeo = data.get("regeocode") or {}
    addr = regeo.get("formatted_address") or ""
    comp = regeo.get("addressComponent") or {}
    return {"ok": True, "address": addr, "component": comp}

# ---------------- 批量运行 / 定时 / 日志 ----------------
@app.post("/api/batch-run")
def api_batch_run(body: dict, _t: None = Depends(require_token)):
    mode = str(body.get("mode", "dry"))
    if mode not in ("dry", "submit"):
        raise HTTPException(status_code=400, detail="mode 仅支持 dry / submit")
    summary, detail = run_all(dry=(mode == "dry"), trigger="批量")
    return {"ok": True, "result": summary, "detail": detail}

@app.post("/api/schedule")
def api_schedule(body: dict, _t: None = Depends(require_token)):
    enabled = bool(body.get("enabled", scheduler_state["enabled"]))
    t = str(body.get("time", scheduler_state["time"])).strip()
    try:
        _parse_hm(t)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler_state["enabled"] = enabled
    scheduler_state["time"] = t
    _mark_past_slot_done()
    nxt_str = _pending_slot(t).strftime("%Y-%m-%d %H:%M:%S") if enabled else None
    scheduler_state["next"] = nxt_str
    save_schedule()
    save_log("定时更新", "定时批量签到设置已更新",
             "enabled=%s time=%s（UTC%+g） 下次执行 %s" % (enabled, t, TZ_OFFSET, nxt_str or "-"))
    _sched_wake.set()
    return {"ok": True, "scheduler": {"enabled": enabled, "time": t, "next": nxt_str}}

@app.get("/api/logs")
def api_logs(limit: int = 50, _t: None = Depends(require_token)):
    limit = max(1, min(limit, 200))
    return {"logs": fetch_logs(limit)}

@app.post("/api/logs/clear")
def api_logs_clear(_t: None = Depends(require_token)):
    clear_logs()
    save_log("日志管理", "日志已清空", "由 Web 页面手动清除")
    return {"ok": True}

# ---------------- 启动 ----------------
init_db()
cleanup_stale_temp_accounts()
_start_scheduler()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
