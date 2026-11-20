# -*- coding: utf-8 -*-
"""
股票日报语音合成脚本（浓缩口播版）

从 reports/ 目录读取当天报告 markdown，提取核心结论拼成口语稿，
调用小米 MiMo 预置音色（默认「苏打」）合成语音，合并输出 mp3，
可选把 mp3 作为附件发一封「语音播报」邮件。

设计约束：
- 零重型依赖：只用标准库 + requests（requirements 已含）。
- 音频用 wav 格式合成，用标准库 wave 模块无损拼接，避免 pydub/ffmpeg 依赖。
- 最终用 ffmpeg（GitHub Actions ubuntu runner 自带）转成 mp3，方便邮件附件。
- 邮件发送用标准库 smtplib，不依赖上游 EmailSender，避免合并冲突。

用法：
    python scripts/tts_report.py              # 自动找当天 reports/*.md，合成 + 发邮件
    python scripts/tts_report.py --date 20261120 --no-email
    python scripts/tts_report.py --voice 白桦 --out reports/voice_report.mp3

环境变量：
    MIMO_API_KEY      必填，小米 MiMo API Key
    EMAIL_SENDER      发件人邮箱（发邮件时必填）
    EMAIL_PASSWORD    邮箱授权码（发邮件时必填）
    EMAIL_RECEIVERS   收件人，逗号分隔（可选，默认发给自己）
    EMAIL_SENDER_NAME 发件人显示名（可选）
"""

import os
import re
import sys
import io
import time
import wave
import base64
import argparse
import subprocess
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")

MIMO_URL = "https://api.xiaomimimo.com/v1/chat/completions"
DEFAULT_VOICE = "苏打"
DEFAULT_STYLE = "请用沉稳、清晰、专业的新闻播报语气朗读以下财经内容，语速适中，数字和代码逐字清晰。"

# SMTP 配置（与上游 email_sender.py 保持一致）
SMTP_CONFIGS = {
    "qq.com": {"server": "smtp.qq.com", "port": 465, "ssl": True},
    "foxmail.com": {"server": "smtp.qq.com", "port": 465, "ssl": True},
    "163.com": {"server": "smtp.163.com", "port": 465, "ssl": True},
    "126.com": {"server": "smtp.126.com", "port": 465, "ssl": True},
    "139.com": {"server": "smtp.139.com", "port": 465, "ssl": True},
    "gmail.com": {"server": "smtp.gmail.com", "port": 587, "ssl": False},
    "outlook.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "hotmail.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "live.com": {"server": "smtp-mail.outlook.com", "port": 587, "ssl": False},
    "sina.com": {"server": "smtp.sina.com", "port": 465, "ssl": True},
    "sohu.com": {"server": "smtp.sohu.com", "port": 465, "ssl": True},
    "aliyun.com": {"server": "smtp.aliyun.com", "port": 465, "ssl": True},
}


def load_api_key():
    key = os.environ.get("MIMO_API_KEY")
    if key:
        return key
    raise ValueError("未设置 MIMO_API_KEY 环境变量")


# ---------------------------------------------------------------- 提取

def strip_emoji(s):
    """去掉 emoji 和装饰符号，避免 TTS 念出乱码。"""
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2190-\u21FF"
               r"\u2705\u274C\u2757\u26A0\u2753\u2754\u2B50\u2605\u2606]", "", s)
    return s


def digitwise(code):
    """6 位代码按位分隔，让 TTS 逐位念（如 688739 -> 6 8 8 7 3 9）。"""
    code = re.sub(r"\D", "", code)
    if len(code) == 6:
        return " ".join(code)
    return code


def clean_inline(s):
    s = strip_emoji(s)
    s = s.replace("**", "").replace("*", "")
    s = s.replace("|", "，")
    # 英文缩写换成中文读法，避免 TTS 乱念
    s = re.sub(r"MA5", "五日均线", s)
    s = re.sub(r"MA10", "十日均线", s)
    s = re.sub(r"MA20", "二十日均线", s)
    s = re.sub(r"RSI", "相对强弱指标", s)
    s = re.sub(r"\bPE\b", "市盈率", s)
    # 涨跌幅正负号转中文
    s = re.sub(r"涨跌幅\s*\+\s*(\d)", r"上涨 \1", s)
    s = re.sub(r"涨跌幅\s*-\s*(\d)", r"下跌 \1", s)
    # 去掉引号（TTS 可能读出引号）
    s = s.replace("“", "").replace("”", "").replace("\"", "").replace("'", "")
    s = re.sub(r"\s*，\s*", "，", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedup_punct(s):
    """把连续多个句号/顿号收敛成单个。"""
    s = re.sub(r"[。；]{2,}", "。", s)
    s = re.sub(r"[，]{2,}", "，", s)
    s = re.sub(r"，，", "，", s)
    s = re.sub(r"。，", "。", s)
    s = re.sub(r"，。", "。", s)
    return s


def extract_dashboard(text):
    """从股票决策仪表盘报告提取浓缩口播。"""
    parts = []
    # 标题日期
    m = re.search(r"#\s*🎯\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        parts.append(f"{m.group(1)} A股决策仪表盘。")

    # 总览行
    m = re.search(r"共分析\s*\*{0,2}(\d+)\*{0,2}\s*只股票[^\n]*", text)
    if m:
        line = clean_inline(m.group(0))
        # 买入:0 观望:2 卖出:1 -> 买入0只，观望2只，卖出1只
        line = re.sub(r"买入\s*[:：]\s*(\d+)", r"买入\1只", line)
        line = re.sub(r"观望\s*[:：]\s*(\d+)", r"观望\1只", line)
        line = re.sub(r"卖出\s*[:：]\s*(\d+)", r"卖出\1只", line)
        parts.append(line + "。")

    # 逐只股票：名称、代码、结论、一句话决策、涨跌幅
    # 股票段落以 "## ⚪/🟠/🔴 名称 (代码)" 开头
    stock_blocks = re.split(r"\n##\s+[^\n]*\((\d{6})\)\s*\n", text)
    # 更稳的方式：逐个匹配 "## ... 名称 (code)"
    for m in re.finditer(r"##\s+[^\n]*?([\u4e00-\u9fffA-Za-z0-9]+)\s*\((\d{6})\)", text):
        name = m.group(1)
        code = m.group(2)
        block = text[m.end():]
        nxt = re.search(r"\n##\s+", block)
        block = block if not nxt else block[:nxt.start()]

        # 结论：**⚪ 观望** | 震荡  →  观望 / 震荡
        concl = ""
        cm = re.search(r"\*\*[^\n]*?([\u4e00-\u9fff]{2,4})\*\*\s*\|?\s*([\u4e00-\u9fff]{2,4})", block)
        if cm:
            concl = f"{cm.group(1)}，{cm.group(2)}"

        # 一句话决策
        decision = ""
        dm = re.search(r"一句话决策\*{0,2}\s*[:：]\s*(.+)", block)
        if dm:
            decision = clean_inline(dm.group(1))

        # 涨跌幅：定位「当日行情」表格的第一行数据
        chg = ""
        qh = re.search(r"当日行情", block)
        if qh:
            after = block[qh.end():]
            # 找第一个数据行（含 | 数字 | ... | ±x% | ... ）
            row = re.search(r"\|\s*([\d.]+)\s*\|[^|\n]*\|[^|\n]*\|[^|\n]*\|[^|\n]*\|\s*([+-]?\d+\.?\d*%)\s*\|", after)
            if row:
                chg = clean_inline(f"收盘 {row.group(1)} 元，涨跌幅 {row.group(2)}")

        seg = f"{name}，代码 {digitwise(code)}。"
        if concl:
            seg += f"结论 {concl}。"
        if decision:
            seg += f"{decision}。"
        if chg:
            seg += f"{chg}。"
        parts.append(seg)

    return _dedup_punct("".join(parts)) if parts else None


def extract_market_review(text):
    """从大盘复盘报告提取浓缩口播。"""
    parts = []
    m = re.search(r"#\s*🎯\s*大盘复盘", text)
    date_m = re.search(r"(\d{4}-\d{2}-\d{2})\s*大盘复盘", text)
    if date_m:
        parts.append(f"{date_m.group(1)} 大盘复盘。")

    # 开篇核心矛盾段（blockquote 里的第一段）
    bm = re.search(r">\s*(.+?)(?:\n|$)", text)
    if bm:
        parts.append(clean_inline(bm.group(1)) + "。")

    # 盘面信号
    sm = re.search(r"盘面信号\*{0,2}\s*[:：]\s*(.+)", text)
    if sm:
        parts.append("盘面信号，" + clean_inline(sm.group(1)) + "。")

    # 操作建议
    om = re.search(r"操作建议\*{0,2}\s*[:：]\s*(.+)", text)
    if om:
        parts.append("操作建议，" + clean_inline(om.group(1)) + "。")

    return _dedup_punct("".join(parts)) if parts else None


# ---------------------------------------------------------------- 合成

def split_long(text, max_chars=260):
    """按句切块，控制在 max_chars 以内。"""
    sents = re.split(r"(?<=[。！？；])", text)
    chunks, cur = [], ""
    for s in sents:
        if not s.strip():
            continue
        if len(cur) + len(s) <= max_chars:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def synthesize_wav(api_key, voice, text, style):
    """合成单段，返回 wav 字节。"""
    payload = {
        "model": "mimo-v2.5-tts",
        "modalities": ["text", "audio"],
        "messages": [
            {"role": "user", "content": style},
            {"role": "assistant", "content": text},
        ],
        "audio": {"voice": voice, "format": "wav"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(1, 5):
        try:
            resp = requests.post(MIMO_URL, headers=headers, json=payload, timeout=120)
            if resp.status_code == 200:
                data = resp.json()
                ao = data.get("choices", [{}])[0].get("message", {}).get("audio")
                if ao and "data" in ao:
                    return base64.b64decode(ao["data"])
                print(f"  ⚠️ 响应无音频: {str(data)[:150]}", flush=True)
            elif resp.status_code == 429:
                wait = 6 * attempt
                print(f"  ⏳ 429 限流，等待 {wait}s", flush=True)
                time.sleep(wait)
            else:
                print(f"  ❌ HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                time.sleep(3)
        except Exception as e:
            print(f"  ⚠️ 异常: {e}", flush=True)
            time.sleep(5)
    return None


def wav_headers_and_data(raw):
    """解析 wav 字节，返回 (params, pcm_bytes)。"""
    bio = io.BytesIO(raw)
    w = wave.open(bio, "rb")
    params = w.getparams()
    frames = w.readframes(w.getnframes())
    w.close()
    return params, frames


def concat_wavs(wav_list):
    """拼接多个 wav 字节，返回合并后的 wav 字节。"""
    if not wav_list:
        return None
    if len(wav_list) == 1:
        return wav_list[0]
    params, first_frames = wav_headers_and_data(wav_list[0])
    all_frames = [first_frames]
    for raw in wav_list[1:]:
        p, f = wav_headers_and_data(raw)
        if p[:3] != params[:3]:
            # 采样率/声道/位深不一致，跳过（正常应一致）
            continue
        all_frames.append(f)
    out = io.BytesIO()
    w = wave.open(out, "wb")
    w.setparams(params)
    for f in all_frames:
        w.writeframes(f)
    w.close()
    return out.getvalue()


def wav_to_mp3(wav_bytes, out_path):
    """用 ffmpeg 转 mp3。"""
    tmp_wav = out_path + ".tmp.wav"
    with open(tmp_wav, "wb") as f:
        f.write(wav_bytes)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", tmp_wav, "-codec:a", "libmp3lame", "-b:a", "128k", out_path],
        capture_output=True, text=True,
    )
    os.remove(tmp_wav)
    return r.returncode == 0


def synth_to_mp3(api_key, voice, speech_text, style, out_path):
    """把口播稿合成 mp3。"""
    chunks = split_long(speech_text, 260)
    print(f"📖 口播稿共 {len(speech_text)} 字，切分 {len(chunks)} 段，音色 {voice}", flush=True)
    wavs = []
    for i, c in enumerate(chunks, 1):
        print(f"[{i}/{len(chunks)}] 合成 ({len(c)}字): {c[:20]}...", flush=True)
        wav = synthesize_wav(api_key, voice, c, style)
        if wav is None:
            print(f"❌ 第 {i} 段失败", flush=True)
            return False
        wavs.append(wav)
        if i < len(chunks):
            time.sleep(1.0)

    merged = concat_wavs(wavs)
    if merged is None:
        return False
    if wav_to_mp3(merged, out_path):
        size = os.path.getsize(out_path)
        print(f"✅ 语音合成完成: {out_path}（{size/1024:.1f} KB）", flush=True)
        return True
    # ffmpeg 失败则退回 wav
    fallback = out_path.replace(".mp3", ".wav")
    with open(fallback, "wb") as f:
        f.write(merged)
    print(f"⚠️ mp3 转换失败，已输出 wav: {fallback}", flush=True)
    return True


# ---------------------------------------------------------------- 邮件发送

def send_voice_email(mp3_path, subject=None):
    """把 mp3 作为附件发一封「语音播报」邮件。"""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email.mime.application import MIMEApplication
    from email.header import Header
    from email.utils import formataddr
    import smtplib

    sender = os.environ.get("EMAIL_SENDER", "")
    password = os.environ.get("EMAIL_PASSWORD", "")
    if not sender or not password:
        print("⚠️ 未配置 EMAIL_SENDER / EMAIL_PASSWORD，跳过发送语音邮件", flush=True)
        return False

    receivers = [r.strip() for r in os.environ.get("EMAIL_RECEIVERS", "").split(",") if r.strip()]
    if not receivers:
        receivers = [sender]
    sender_name = os.environ.get("EMAIL_SENDER_NAME", "daily_stock_analysis股票分析助手")

    if subject is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
        subject = f"📢 股票语音播报 - {date_str}"

    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender))
    msg["To"] = ", ".join(receivers)

    body = "今日股票语音播报（浓缩口播版）见附件。\n\n由小米 MiMo 预置音色合成。"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with open(mp3_path, "rb") as f:
        audio_data = f.read()
    part = MIMEApplication(audio_data, _subtype="mp3")
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(mp3_path))
    msg.attach(part)

    domain = sender.split("@")[-1].lower()
    cfg = SMTP_CONFIGS.get(domain, {"server": f"smtp.{domain}", "port": 465, "ssl": True})

    server = None
    try:
        if cfg["ssl"]:
            server = smtplib.SMTP_SSL(cfg["server"], cfg["port"], timeout=30)
        else:
            server = smtplib.SMTP(cfg["server"], cfg["port"], timeout=30)
            server.starttls()
        server.login(sender, password)
        server.send_message(msg)
        print(f"✅ 语音邮件已发送，收件人: {receivers}", flush=True)
        return True
    except Exception as e:
        print(f"❌ 发送语音邮件失败: {e}", flush=True)
        return False
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="日期 YYYYMMDD，默认当天")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--style", default=DEFAULT_STYLE)
    ap.add_argument("--out", default=None, help="输出 mp3 路径")
    ap.add_argument("--no-email", action="store_true", help="只合成，不发邮件")
    args = ap.parse_args()

    api_key = load_api_key()

    date_str = args.date or datetime.now().strftime("%Y%m%d")
    # 报告文件名用 YYYYMMDD
    stock_md = os.path.join(REPORTS_DIR, f"report_{date_str}.md")
    market_md = os.path.join(REPORTS_DIR, f"market_review_{date_str}.md")

    speech_parts = []
    if os.path.exists(stock_md):
        with open(stock_md, "r", encoding="utf-8") as f:
            t = extract_dashboard(f.read())
            if t:
                speech_parts.append(t)
    if os.path.exists(market_md):
        with open(market_md, "r", encoding="utf-8") as f:
            t = extract_market_review(f.read())
            if t:
                speech_parts.append(t)

    if not speech_parts:
        print(f"❌ 未找到 {date_str} 的报告（report_*.md / market_review_*.md），跳过语音合成", flush=True)
        sys.exit(0)

    speech_text = "。".join(speech_parts)
    out_path = args.out or os.path.join(REPORTS_DIR, f"voice_report_{date_str}.mp3")

    if not synth_to_mp3(api_key, args.voice, speech_text, args.style, out_path):
        sys.exit(1)

    print(f"口播稿预览：{speech_text[:200]}...", flush=True)

    if not args.no_email:
        send_voice_email(out_path)


if __name__ == "__main__":
    main()
