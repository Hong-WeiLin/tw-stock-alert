"""台股供應鏈重點監控：依題材關鍵字篩選新聞與重大訊息，對應台灣個股後發到 Discord。
僅用標準函式庫。環境變數：DISCORD_WEBHOOK（必填）、DRY_RUN=1（只印不發）。
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TZ = timezone(timedelta(hours=8))
WINDOW_MIN = 20          # 只看最近 N 分鐘內發布的消息
MAX_PER_RUN = 8          # 每次最多發幾則
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")
UA = {"User-Agent": "Mozilla/5.0 (tw-alert)"}

# 事件關鍵字：標題必須含其中之一（代表有實質供需、訂單、產能、政策變化）
EVENT = [
    "停產", "減產", "擴產", "擴廠", "新廠", "缺貨", "漲價", "調漲", "報價", "砍單", "追單", "拉貨", "急單",
    "斷料", "斷鏈", "供不應求", "供應吃緊", "交期", "接單", "訂單", "量產", "出貨", "送樣", "認證", "下修",
    "上修", "財測", "展望", "法說", "出口管制", "禁令", "關稅", "制裁", "停工", "火災", "地震", "跳電",
    "罷工", "收購", "併購", "資本支出", "目標價", "庫存", "稼動率", "產能",
]
# 一律排除的雜訊：行情流水帳、籌碼、配息、人事
EXCLUDE = [
    "漲停", "跌停", "填息", "翻倍", "強勢", "領漲", "領跌", "買超", "賣超", "融資", "融券", "技術面",
    "殖利率", "ETF", "除息", "配息", "股東會", "人事", "發言人", "開高", "開低", "收盤", "盤後", "當沖",
    "飆股", "噴出",
]
# 題材：(關鍵字, 相關個股)。命中關鍵字就帶出對應的台灣個股。可自行增減。
THEMES = {
    "被動元件/MLCC": (
        ["MLCC", "村田", "太陽誘電", "TDK", "被動元件", "積層陶瓷", "晶片電阻", "鉭質電容"],
        ["2327 國巨", "2492 華新科", "3026 禾伸堂", "6173 信昌電", "2456 奇力新"]),
    "記憶體": (
        ["DRAM", "NAND", "記憶體", "HBM", "美光", "海力士", "快閃記憶體"],
        ["2408 南亞科", "2344 華邦電", "2337 旺宏", "3260 威剛", "8299 群聯"]),
    "AI伺服器/散熱": (
        ["AI伺服器", "AI 伺服器", "GB200", "GB300", "液冷", "水冷", "散熱", "輝達", "NVIDIA", "機櫃"],
        ["2382 廣達", "3231 緯創", "6669 緯穎", "2376 技嘉", "3017 奇鋐", "3324 雙鴻", "2317 鴻海"]),
    "半導體/先進封裝": (
        ["台積電", "CoWoS", "先進封裝", "2奈米", "3奈米", "晶圓代工", "矽晶圓", "ABF"],
        ["2330 台積電", "3711 日月光投控", "6488 環球晶", "3443 創意", "3661 世芯-KY"]),
    "PCB/載板/CCL": (
        ["PCB", "載板", "銅箔基板", "CCL", "玻纖布", "T-glass", "低CTE", "覆銅板"],
        ["3037 欣興", "8046 南電", "3189 景碩", "2383 台光電", "6213 聯茂", "1815 富喬"]),
    "光通訊/矽光子": (
        ["光通訊", "矽光子", "CPO", "光收發", "800G", "1.6T", "光模組"],
        ["4979 華星光", "3363 上詮", "3450 聯鈞", "3081 聯亞", "4977 眾達-KY"]),
    "面板": (
        ["面板", "LCD", "OLED", "友達", "群創", "電視面板"],
        ["2409 友達", "3481 群創"]),
    "航運": (
        ["貨櫃", "運價", "航運", "SCFI", "紅海", "運費"],
        ["2603 長榮", "2609 陽明", "2615 萬海"]),
    "手機/晶片設計": (
        ["聯發科", "手機晶片", "高通", "蘋果供應鏈", "iPhone"],
        ["2454 聯發科", "2317 鴻海", "3008 大立光"]),
}
QUERIES = [
    "MLCC OR 被動元件 OR 村田 停產 OR 漲價 OR 缺貨",
    "DRAM OR NAND OR 記憶體 漲價 OR 報價 OR 缺貨",
    "AI伺服器 OR 液冷 OR 散熱 出貨 OR 訂單 OR 供應",
    "台積電 OR CoWoS OR 先進封裝 擴產 OR 產能 OR 訂單",
    "PCB OR 載板 OR 銅箔基板 OR 玻纖布 漲價 OR 缺貨 OR 擴產",
    "光通訊 OR 矽光子 OR CPO 出貨 OR 訂單 OR 量產",
    "面板 OR 貨櫃 OR 航運 報價 OR 運價 OR 減產",
    "台股 供應鏈 停產 OR 缺貨 OR 砍單 OR 追單",
    "出口管制 OR 關稅 台灣 半導體 OR 電子 供應鏈",
]
MOPS_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
]
MOPS_KEEP = ["訂單", "合約", "擴產", "擴廠", "停工", "火災", "減產", "停產", "重大", "收購", "併購", "資本支出"]


def get(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def stocks_for(text):
    out = []
    for kws, stocks in THEMES.values():
        if any(k.lower() in text.lower() for k in kws):
            for s in stocks:
                if s not in out:
                    out.append(s)
    for stocks in (v[1] for v in THEMES.values()):
        for s in stocks:
            name = s.split(" ", 1)[1]
            if name in text and s not in out:
                out.append(s)
    return out[:8]


def keep_news(title):
    if any(k in title for k in EXCLUDE):
        return False
    if not any(k in title for k in EVENT):
        return False
    return bool(stocks_for(title))


def fetch_news(now):
    items = []
    for q in QUERIES:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:1h")
               + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
        try:
            root = ET.fromstring(get(url))
        except Exception as e:
            print("rss failed:", q, e, file=sys.stderr)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            src = (it.findtext("source") or "").strip()
            try:
                t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(TZ)
            except Exception:
                continue
            if now - t > timedelta(minutes=WINDOW_MIN) or t > now + timedelta(minutes=5):
                continue
            if not keep_news(title):
                continue
            items.append({"id": link or title, "title": title, "body": f"來源：{src or '新聞'}（關鍵字自動篩選，詳見原文）",
                          "link": link, "time": t, "stocks": stocks_for(title)})
    return items


def fetch_mops(now):
    items = []
    for u in MOPS_URLS:
        try:
            rows = json.loads(get(u))
        except Exception as e:
            print("mops failed:", u, e, file=sys.stderr)
            continue
        for r in rows:
            d = str(r.get("發言日期", "")).strip()
            tm = str(r.get("發言時間", "")).strip().zfill(6)
            try:
                y = int(d[:-4]) + (1911 if len(d) == 7 else 0)
                t = datetime(y, int(d[-4:-2]), int(d[-2:]), int(tm[:2]), int(tm[2:4]), int(tm[4:6]), tzinfo=TZ)
            except Exception:
                continue
            if now - t > timedelta(minutes=WINDOW_MIN) or t > now + timedelta(minutes=5):
                continue
            code = str(r.get("公司代號", "")).strip()
            name = str(r.get("公司名稱", "")).strip()
            subj = str(r.get("主旨", "")).strip()
            body = str(r.get("說明", "")).strip().replace("\r", " ").replace("\n", " ")
            if any(k in subj for k in EXCLUDE) or not any(k in subj for k in MOPS_KEEP):
                continue
            items.append({"id": f"mops-{code}-{d}{tm}-{subj[:30]}", "title": f"{code} {name}：{subj}",
                          "body": "重大訊息｜" + body[:200], "link": "https://mops.twse.com.tw/mops/web/t05sr01_1",
                          "time": t, "stocks": [f"{code} {name}"]})
    return items


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(seen):
    cutoff = time.time() - 2 * 86400
    seen = {k: v for k, v in seen.items() if v > cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)


def post(webhook, content):
    data = json.dumps({"content": content[:1990]}).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={**UA, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
    time.sleep(1)


def session_label(t):
    m = t.hour * 60 + t.minute
    if m < 8 * 60 + 30:
        return "盤前"
    if m < 13 * 60 + 30:
        return "盤中" if m >= 9 * 60 else "盤前"
    if m < 18 * 60:
        return "盤後"
    return "夜間"


def main():
    now = datetime.now(TZ)
    dry = os.environ.get("DRY_RUN") == "1"
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook and not dry:
        sys.exit("缺少 DISCORD_WEBHOOK")
    seen = load_seen()
    out = []
    for it in fetch_mops(now) + fetch_news(now):
        if it["id"] in seen:
            continue
        seen[it["id"]] = time.time()
        out.append(it)
    out.sort(key=lambda x: x["time"])
    print(f"符合 {len(out)} 則")
    for it in out[:MAX_PER_RUN]:
        head = f"{now.month}/{now.day} {session_label(now)}重要補充｜{now:%H:%M} 查核"
        msg = "\n".join([
            head,
            f"▪️ 重點標題｜{it['title']}",
            f"重點內文｜{it['body']}",
            "相關個股｜" + "、".join(it["stocks"]),
            f"{it['time']:%m/%d %H:%M} 發布　{it['link']}",
        ])
        if dry:
            print(msg, "\n---")
        else:
            post(webhook, msg)
    if not dry:
        save_seen(seen)


if __name__ == "__main__":
    main()
