# -*- coding: utf-8 -*-
"""抓取真机 deviceId（本地代理抓包 + 自动解密）

用途
----
今日校园把设备的 deviceId 放在请求头 CpdailyInfo / Cpdaily-Extension 里，
并用 DES 加密。本脚本在本机起一个 HTTP 代理，手机设好代理后操作一次 App，
脚本会自动解析经过的请求，把 deviceId 解出来。

为什么需要真机 deviceId
----------------------
服务端会校验「提交的 deviceId == 该账号在 App 里授权过的设备」。
用程序生成的随机 UUID 提交，会返回 2210990000 签到失败；
用真机 deviceId 提交才会进入正常的业务校验。

用法
----
1) 电脑上运行（默认监听 8888）：
       python scripts/fetch_real_deviceid.py
   指定端口：  python scripts/fetch_real_deviceid.py --port 8889

2) 手机与电脑连同一个 WiFi，手机 WiFi 设置里配置「手动代理」：
       主机名 = 电脑的局域网 IP（脚本启动时会打印）
       端口   = 8888

3) 打开手机上的「今日校园」App，随便点几下（比如进首页、看签到列表）。
   脚本会自动捕获并解密，看到 deviceId 后即成功。

4) 关于 HTTPS：
   今日校园的接口基本都是 HTTPS，明文抓取需要中间人证书，本脚本不做 MITM。
   推荐两种更省事的做法（任选其一）：

   【A. 用现成抓包工具（最省事）】
     - Android：装 HttpCanary / Reqable / Charles，在手机端直接抓
     - iOS：装 Stream / Charles
     抓包后找到任意一条带 CpdailyInfo 头的请求，复制头的值，
     用下面的命令解出来：
         python scripts/extract_deviceid.py "<CpdailyInfo 头的值>"

   【B. 用本脚本抓 HTTP 明文】
     若某些接口走明文 HTTP（少数老版本/特定路径），本脚本可直接解出。

5) 抓到后 Ctrl+C 退出，记得把手机代理关掉。

说明
----
- 只解析今日校园相关的请求头，不会记录你的账号密码和正文内容。
- HTTPS 请求本脚本会提示「需装证书」，但不会解密（不做 MITM）。
- 证书/抓包工具请自行安装，用完记得删除手机上的信任与代理。
"""
import argparse
import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

try:
    from Crypto.Cipher import DES
    from Crypto.Util.Padding import unpad
except ImportError:
    print("缺少依赖 pycryptodome，请先执行：")
    print("  jrxy-engine\\.venv\\Scripts\\pip install pycryptodome")
    sys.exit(1)

IV = bytes(range(1, 9))
KEYS = {
    "info": (b"ST83=@XV", "CpdailyInfo"),
    "ext": (b"b3L26XNL", "Cpdaily-Extension"),
    "token": (b"XCE927==", "sessionToken"),
}

SEEN = set()
LOCK = threading.Lock()
OUT_DIR = Path(__file__).resolve().parent.parent / "deviceid_capture"


def log(msg):
    print(msg, flush=True)


def local_ip():
    """取本机在局域网中的 IP（不真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def try_decrypt(value: str, key: bytes):
    """解密一个头的值；失败返回 None。"""
    v = (value or "").strip()
    if not v:
        return None
    # 有的客户端会带 base64 的换行/空格
    v = v.replace("\r", "").replace("\n", "").replace(" ", "")
    # 补齐 base64 padding
    pad = (-len(v)) % 4
    if pad:
        v += "=" * pad
    try:
        raw = base64.b64decode(v, validate=False)
    except Exception:
        return None
    if not raw or len(raw) % 8:
        return None
    try:
        pt = DES.new(key, DES.MODE_CBC, IV).decrypt(raw)
    except Exception:
        return None
    # 去 PKCS5 padding：严格解一次，失败再手工裁掉尾部填充字节
    try:
        pt = unpad(pt, DES.block_size)
    except Exception:
        last = pt[-1] if pt else 0
        if 1 <= last <= 8 and pt.endswith(bytes([last]) * last):
            pt = pt[:-last]
    try:
        text = pt.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 去掉 JSON 之外的尾部控制字符，避免 json.loads 失败
    text = text.strip().strip("\x00").rstrip()
    if text and text[-1] not in "}>" and "{" in text:
        cut = text.rfind("}")
        if cut != -1:
            text = text[:cut + 1]
    return text if text.strip() else None


def parse_device_json(text: str):
    """从解密结果里抠出 deviceJSON。"""
    text = text.strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 有些版本是 key=value&... 形式
    if "=" in text and "{" not in text:
        out = {}
        for part in text.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out or None
    # 兜底：用正则抓 deviceId
    import re
    m = re.search(r'"deviceId"\s*:\s*"([^"]+)"', text) or \
        re.search(r'deviceId["\s:=]+([0-9a-fA-F-]{16,})', text)
    if m:
        return {"deviceId": m.group(1)}
    return None


def handle_headers(headers: dict, tag: str):
    """检查一组请求头，命中就解密并打印。"""
    for kind, (key, hname) in KEYS.items():
        raw = headers.get(hname.lower())
        if not raw:
            continue
        text = try_decrypt(raw, key)
        if not text:
            continue
        obj = parse_device_json(text)
        if not obj:
            continue
        did = obj.get("deviceId") or obj.get("device_id") or ""
        if not did:
            continue
        with LOCK:
            if did in SEEN:
                return
            SEEN.add(did)
        log("")
        log("=" * 64)
        log("★ 抓到真机 deviceId: %s" % did)
        log("=" * 64)
        for k, v in obj.items():
            log("  %-14s = %s" % (k, v))
        log("")
        log("已保存到: %s" % OUT_DIR)
        log("下一步：把上面这串 deviceId 填到账号配置里（编辑账号 → deviceId）")
        log("        然后点「探测」验证，确认能正常登录后再签到。")
        log("")
        # 落盘
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            (OUT_DIR / ("deviceId_%s.txt" % did[:8])).write_text(did + "\n", encoding="utf-8")
            (OUT_DIR / ("decoded_%s.json" % did[:8])).write_text(
                json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log("（保存失败: %s）" % e)


def read_http_head(sock):
    """读一个 HTTP 请求头，返回 (raw_bytes, headers_dict, first_line)。"""
    buf = b""
    sock.settimeout(20)
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return None, None, None
        if not chunk:
            return None, None, None
        buf += chunk
        if len(buf) > 65536:
            break
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines:
        return None, None, None
    first = lines[0].decode("latin-1", "replace")
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    return head, headers, first


def handle_client(conn, addr):
    try:
        head, headers, first = read_http_head(conn)
        if not headers:
            return
        # 先看是不是 CONNECT（HTTPS）
        if first.upper().startswith("CONNECT"):
            host = first.split()[1] if len(first.split()) > 1 else "?"
            tip = ("[HTTPS] %s → 需要先在手机安装并信任证书才能看到明文"
                   % host)
            with LOCK:
                if host not in SEEN:
                    SEEN.add(host)
                    log(tip)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            return
        # 普通 HTTP 请求：直接解析头
        handle_headers(headers, first)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(port):
    ip = local_ip()
    log("=" * 64)
    log("  真机 deviceId 抓取")
    log("=" * 64)
    log("")
    log("1) 手机和电脑连同一个 WiFi")
    log("2) 手机 WiFi 设置 → 手动代理：")
    log("      主机名: %s" % ip)
    log("      端口  : %d" % port)
    log("3) 打开手机「今日校园」App，随便点几下")
    log("")
    log("注意：App 接口多为 HTTPS，明文抓取需要 MITM 证书（本脚本不提供）。")
    log("      若想省事，建议直接用手机端抓包工具（HttpCanary/Reqable/Stream），")
    log("      导出 CpdailyInfo 头后执行：")
    log("        python scripts/extract_deviceid.py \"<头的值>\"")
    log("")
    log("本脚本对走明文 HTTP 的请求可直接解密出 deviceId。")
    log("")
    log("等待请求中…（Ctrl+C 退出）")
    log("")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(64)
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log("\n已停止。记得把手机上的代理关掉。")
    finally:
        srv.close()


def main():
    ap = argparse.ArgumentParser(description="抓取真机 deviceId（本地代理）")
    ap.add_argument("--port", type=int, default=8888, help="监听端口，默认 8888")
    args = ap.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
