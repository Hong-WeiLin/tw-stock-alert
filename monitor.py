"""台股即時重點監控：抓重大訊息與財經新聞，關鍵字篩選後發到 Discord。
僅用標準函式庫。環境變數：DISCORD_WEBHOOK（必填）、DRY_RUN=1（只印不發）。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TZ = timezone(timedelta(hours=8))
WINDOW_MIN = 20          # 只看最近 N 分鐘內發布的消息
MAX_PER_RUN = 10         # 每次最多發幾則
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")
UA = {"User-Agent": "Mozilla/5.0 (tw-alert)"}

INCLUDE = [
    "營收", "財報", "獲利", "虧損", "EPS", "法說", "訂單", "接單", "漲價", "調漲", "降價", "調降",
    "減產", "停產", "擴產", "投資", "併購", "合併", "收購", "增資", "減資", "罰款", "裁罰", "裁定",
    "斷鏈", "缺貨", "供不應求", "庫存", "下修", "上修", "目標價", "出貨", "供應", "禁令", "關稅",
    "出口管制", "制裁", "夜盤", "台指期", "費半", "那斯達克", "道瓊", "聯準會", "升息", "降息",
    "停工", "火災", "地震", "跳電", "重大", "解約", "違約", "下市", "處分", "取得", "股利", "配息",
]
EXCLUDE = [
    "董事長異動", "人事異動", "經理人", "發言人", "代理發言人", "委任", "解任", "薪酬委員會",
    "股東常會", "股東會召開", "澄清媒體", "更正", "財務主管", "公司治理主管", "內部稽核",
]
QUERIES = [
    "台股 盤前", "台指期 夜盤", "美股 收盤 費城半導體", "台積電 供應鏈 漲價", "台灣 公司 法說會 營收",
    "記憶體 DRAM 報價", "AI 伺服器 供應鏈 訂單", "面板 報價", "航運 運價", "出口管制 台灣 半導體",
]
MOPS_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O",
]
COMPANY_URLS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
    "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O",
]


def get(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_companies():
    names = {}
    for u in COMPANY_URLS:
        try:
            for row in json.loads(get(u)):
                code = (row.get("公司代號") or row.get("SecuritiesCompanyCode") or "").strip()
                name = (row.get("公司簡稱") or row.get("CompanyAbbreviation") or "").strip()
                if code and len(name) >= 2:
                    names[name] = code
        except Exception as e:
            print("company list failed:", u, e, file=sys.stderr)
    return names


def tag_stocks(text, companies):
    found = []
    for m in re.finditer(r"[（(](\d{4})[）)]", text):
        found.append(m.group(1))
    for name, code in companies.items():
        if len(name) >= 3 or name in ("台積電", "聯電", "鴻海", "聯發科", "廣達", "緯創", "長榮", "陽明"):
            if name in text and code not in found:
                found.append(code)
    inv = {c: n for n, c in companies.items()}
    return [f"{c} {inv.get(c, '')}".strip() for c in found[:5]]


def keep(text):
    if any(k in text for k in EXCLUDE):
        return False
    return any(k in text for k in INCLUDE)


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
            pub = it.findtext("pubDate")
            try:
                t = parsedate_to_datetime(pub).astimezone(TZ)
            except Exception:
                continue
            if now - t > timedelta(minutes=WINDOW_MIN) or t > now + timedelta(minutes=5):
                continue
            items.append({"id": link or title, "title": title, "body": "", "link": link, "time": t,
                          "src": "新聞"})
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
            items.append({"id": f"mops-{code}-{d}{tm}-{subj[:30]}", "title": f"{code} {name}：{subj}",
                          "body": body[:200], "link": "https://mops.twse.com.tw/mops/web/t05sr01_1",
                          "time": t, "src": "重大訊息", "stocks": [f"{code} {name}"]})
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


def main():
    now = datetime.now(TZ)
    dry = os.environ.get("DRY_RUN") == "1"
    webhook = os.environ.get("DISCORD_WEBHOOK", "")
    if not webhook and not dry:
        sys.exit("缺少 DISCORD_WEBHOOK")
    companies = load_companies()
    seen = load_seen()
    items = fetch_mops(now) + fetch_news(now)
    out = []
    for it in items:
        if it["id"] in seen:
            continue
        seen[it["id"]] = time.time()
        if not keep(it["title"] + " " + it["body"]):
            continue
        out.append(it)
    out.sort(key=lambda x: x["time"])
    print(f"抓到 {len(items)} 則，符合 {len(out)} 則")
    for it in out[:MAX_PER_RUN]:
        stocks = it.get("stocks") or tag_stocks(it["title"], companies)
        lines = [f"**【{it['src']}】{it['title']}**"]
        if it["body"]:
            lines.append(it["body"])
        if stocks:
            lines.append("相關：" + "、".join(stocks))
        lines.append(f"{it['time']:%m/%d %H:%M}　{it['link']}")
        msg = "\n".join(lines)
        if dry:
            print(msg, "\n---")
        else:
            post(webhook, msg)
    if not dry:
        save_seen(seen)


if __name__ == "__main__":
    main()
