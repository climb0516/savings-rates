# Created by 青云 on 2026/10/21.
"""存款利率 & 储蓄国债看板 数据抓取。只用标准库。

- 大行挂牌利率 + 历次调息：中国银行利率表栏目（逐期页面），农业银行利率页交叉核对
- 储蓄国债各期：中国银行「公告信息」里的储蓄国债销售公告
- 大额存单：中国银行「个人大额存单」发售公告
- LPR：中国外汇交易中心（限流严格，每次只请求一次）
- 新闻：Google News RSS
"""
import html, json, re, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta, date
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
BOC = "https://www.boc.cn"
TERMS = ["活期", "3个月", "6个月", "1年", "2年", "3年", "5年"]
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def fetch(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def text_of(h):
    h = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h)))


def cn_num(s):
    """「十一」「七」「12」 → int"""
    if s.isdigit():
        return int(s)
    if len(s) == 2 and s[0] == "十":
        return 10 + CN_NUM[s[1]]
    if len(s) == 2 and s[1] == "十":
        return CN_NUM[s[0]] * 10
    return CN_NUM.get(s, 0)


# ── 挂牌利率 ─────────────────────────────────────────────
DEPOSIT_RE = re.compile(r"整存整取\s*三个月\s*([\d.]+)\s*(?:半年|六个月)\s*([\d.]+)\s*一年\s*([\d.]+)\s*二年\s*([\d.]+)"
                        r"\s*三年\s*([\d.]+)\s*五年\s*([\d.]+)")
DEMAND_RE = re.compile(r"活期(?:存款)?\s*([\d.]+)")


def parse_deposit(t):
    m, d = DEPOSIT_RE.search(t), DEMAND_RE.search(t)
    if not m or not d:
        return None
    return dict(zip(TERMS, [float(d.group(1))] + [float(x) for x in m.groups()]))


def boc_deposit(old_history):
    """中国银行利率表栏目：列表页拿到每期链接，只抓本地还没有的期（历史表不会变）。"""
    lst = fetch(f"{BOC}/fimarkets/lilv/fd31/")
    links = re.findall(r'href="\./(\d{6}/t\d+_\d+\.html)"[^>]*title="人民币存款利率表(\d{4}-\d{2}-\d{2})"', lst)
    known = {h["date"]: h for h in old_history}
    out = []
    for path, day in links:
        if day in known:
            out.append(known[day])
            continue
        try:
            rates = parse_deposit(text_of(fetch(f"{BOC}/fimarkets/lilv/fd31/{path}")))
        except Exception:
            rates = None
        if rates:
            out.append({"date": day, "rates": rates, "url": f"{BOC}/fimarkets/lilv/fd31/{path}"})
    if not out:
        raise ValueError("no tables")
    return sorted(out, key=lambda x: x["date"], reverse=True)


def abc_deposit():
    t = text_of(fetch("https://www.abchina.com/cn/PersonalServices/Quotation/bwbll/"))
    rates = parse_deposit(t)
    m = re.search(r"本表利率自(\d{4})年(\d+)月(\d+)日起执行", t)
    if not rates:
        raise ValueError("parse failed")
    return {"rates": rates, "date": f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None}


# ── 中国银行公告：储蓄国债 + 大额存单 ─────────────────────
def boc_notices(pages=4):
    items = []
    for i in range(pages):
        url = f"{BOC}/pbservice/bi2/" + ("" if i == 0 else f"index_{i}.html")
        try:
            h = fetch(url)
        except Exception:
            break
        items += [(f"{BOC}/pbservice/bi2/{p}", title) for p, title in
                  re.findall(r'href="\./(\d{6}/t\d+_\d+\.html)"[^>]*title="([^"]+)"', h)]
    return items


def parse_bond(url, title, t):
    year = int(re.search(r"(\d{4})年", title).group(1))
    kind = "凭证式" if "凭证式" in title else "电子式"
    m = re.search(r"将?于(\d{4})年(\d+)月(\d+)日至(?:(\d{4})年)?(\d+)月(\d+)日", t)
    start = end = None
    if m:
        y1, mo1, d1, y2, mo2, d2 = m.groups()
        start = f"{y1}-{int(mo1):02d}-{int(d1):02d}"
        end = f"{y2 or y1}-{int(mo2):02d}-{int(d2):02d}"
    items = []
    for no, term, rate in re.findall(r"第([一二三四五六七八九十\d]+)期[^。；]{0,30}?期限([一二三四五\d])年，?票面年利率为?([\d.]+)%", t):
        items.append({"no": cn_num(no), "term": cn_num(term), "rate": float(rate)})
    if not items:
        return None
    body = re.search(r"尊敬的客户[:：]?\s*(.*?特此公告)", t, re.S)      # 公告正文，给详情页展示
    return {"year": year, "kind": kind, "start": start, "end": end, "items": items, "title": title, "url": url,
            "text": re.sub(r"\s+", " ", body.group(1)).strip()[:800] if body else ""}


def parse_cd(url, title, t):
    """大额存单公告：取「常规产品」那一段的各期限利率。"""
    seg = t[t.find("常规产品"):]
    stop = re.search(r"\d号产品|注\s*[:：]", seg)
    seg = seg[:stop.start()] if stop else seg[:600]
    rates = {}
    for term, rate in re.findall(r"(\d+个月|\d年期)\s+(?:\d+\s+)?([\d.]+)%", seg):
        rates[term.replace("年期", "年")] = float(rate)
    # 表格里起点金额紧跟在第一个期限后面：「1个月 20 0.80%」
    m = re.search(r"\d+个月\s+(\d+)\s+[\d.]+%", seg) or re.search(r"(\d+)\s*万元起", t)
    d = re.search(r"于(\d{4})年(\d+)月(\d+)日发售", t)
    if not rates:
        return None
    return {"title": title, "url": url, "rates": rates, "min_amount": int(m.group(1)) * 10000 if m else 200000,
            "date": f"{d.group(1)}-{int(d.group(2)):02d}-{int(d.group(3)):02d}" if d else None}


def boc_bonds_and_cd(old):
    notices = boc_notices()
    known = {b["url"]: b for b in old.get("bonds", [])}
    bonds = []
    for url, title in notices:
        if "储蓄国债" not in title or "销售公告" not in title:
            continue
        if url in known and "text" in known[url]:          # 旧数据没有正文的重新解析一次
            bonds.append(known[url])
            continue
        try:
            b = parse_bond(url, title, text_of(fetch(url)))
            if b:
                bonds.append(b)
        except Exception:
            pass
    cd = old.get("cd")
    cd_notice = next(((u, t) for u, t in notices if "大额存单" in t and "发售" in t), None)
    if cd_notice and (not cd or cd.get("url") != cd_notice[0]):
        try:
            cd = parse_cd(cd_notice[0], cd_notice[1], text_of(fetch(cd_notice[0]))) or cd
        except Exception:
            pass
    if not bonds:
        raise ValueError("no bond notices")
    # 与旧数据合并（列表页只覆盖最近几页）
    merged = {b["url"]: b for b in old.get("bonds", [])}
    merged.update({b["url"]: b for b in bonds})
    bonds = sorted(merged.values(), key=lambda b: b.get("start") or "", reverse=True)
    return bonds, cd


# ── LPR ─────────────────────────────────────────────────
def lpr(old):
    end = datetime.now(CST).date()
    q = urllib.parse.urlencode({"lang": "CN", "strStartDate": (end - timedelta(days=330)).isoformat(), "strEndDate": end.isoformat()})
    d = json.loads(fetch("https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis?" + q,
                         headers={"Referer": "https://www.chinamoney.com.cn/chinese/bklpr/"}))
    hist = {r["showDateCN"]: {"1Y": float(r["1Y"]), "5Y": float(r["5Y"])} for r in d["records"] if r.get("1Y")}
    merged = {**{h["date"]: {"1Y": h["1Y"], "5Y": h["5Y"]} for h in old.get("lpr", [])}, **hist}
    return [{"date": k, **v} for k, v in sorted(merged.items(), reverse=True)]


# ── 新闻 ────────────────────────────────────────────────
def news():
    out = []
    for q in ("储蓄国债 when:30d", "存款利率 下调 OR 上调 银行 when:30d", "大额存单 利率 when:30d"):
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"})
        root = ET.fromstring(fetch(url))
        for it in root.iter("item"):
            title, src = it.findtext("title") or "", it.find("source")
            source = (src.text or "") if src is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            try:
                t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc).isoformat()
            except Exception:
                continue
            out.append({"title": html.unescape(title), "url": it.findtext("link"), "source": source, "time": t,
                        "topic": "国债" if "国债" in q else "大额存单" if "大额" in q else "存款"})
    seen, uniq = set(), []
    for n in sorted(out, key=lambda x: x["time"], reverse=True):
        key = re.sub(r"\W", "", n["title"])[:18]
        if key not in seen:
            seen.add(key)
            uniq.append(n)
    return uniq[:40]


def main():
    old = {}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text("utf-8"))
        except Exception:
            old = {}
    now = datetime.now(CST).isoformat(timespec="seconds")
    sources = []

    def run(name, fn, *args):
        try:
            res = fn(*args)
            sources.append({"name": name, "ok": True, "updated": now})
            return res
        except Exception as e:
            prev = next((s for s in old.get("sources", []) if s["name"] == name), {})
            sources.append({"name": name, "ok": False, "error": str(e)[:120], "updated": prev.get("updated")})
            return None

    history = run("中国银行 利率表", boc_deposit, old.get("deposit_history", [])) or old.get("deposit_history", [])
    abc = run("农业银行 利率表", abc_deposit) or old.get("abc")
    bc = run("中国银行 国债/大额存单公告", boc_bonds_and_cd, old)
    bonds, cd = bc if bc else (old.get("bonds", []), old.get("cd"))
    lpr_hist = run("外汇交易中心 LPR", lpr, old) or old.get("lpr", [])
    news_items = run("Google News", news) or old.get("news", [])

    # 调息提醒：中行利率表出现新的一期时，记录检测日期，前端据此高亮
    latest = history[0]["date"] if history else None
    change = old.get("rate_change") or {}
    if latest and old.get("deposit_history") and latest != old["deposit_history"][0]["date"]:
        change = {"date": latest, "detected": now}

    others = json.loads((ROOT / "others.json").read_text("utf-8")) if (ROOT / "others.json").exists() else {}
    for s in sources:
        if s["name"] == "中国银行 利率表":
            s["count"] = len(history)
        if s["name"].startswith("中国银行 国债"):
            s["count"] = len(bonds)

    data = {
        "updated": now,                       # 每次都变 = 心跳，保证每次运行都有提交
        "deposit": history[0] if history else None,
        "deposit_history": history,
        "abc": abc,
        "rate_change": change,
        "cd": cd,
        "bonds": bonds,
        "lpr": lpr_hist,
        "news": news_items,
        "others": others,
        "sources": sources,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print("deposit", data["deposit"] and (data["deposit"]["date"], data["deposit"]["rates"]))
    print("history", [h["date"] for h in history])
    print("abc", abc)
    print("cd", cd and (cd["date"], cd["rates"]))
    print("bonds", [(b["start"], b["kind"], [(i["no"], i["term"], i["rate"]) for i in b["items"]]) for b in bonds])
    print("lpr", lpr_hist[:2])
    print("news", len(news_items), "failed", [s["name"] for s in sources if not s["ok"]])


if __name__ == "__main__":
    main()
