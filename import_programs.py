#!/usr/bin/env python3
"""公式サイトと各協会サイトから定席4軒の番組を取得し、ProgramData.json を作る。

取得元
  鈴本演芸場    https://rakugo.or.jp/            （公式）
  新宿末廣亭    http://suehirotei.com/           （公式）
  浅草演芸ホール https://www.asakusaengei.com/     （公式・日付ごとのページ）
  池袋演芸場    落語協会 https://www.rakugo-kyokai.jp/joseki/ikebukuro
                落語芸術協会 https://www.geikyo.com/schedule/index
  演者の所属・プロフィールURL・公式リンク：両協会の芸人紹介ページ

使い方
  python3 import_programs.py                  # 取得して data/ProgramData.json を書き換える
  python3 import_programs.py --refresh        # キャッシュを使わずに取り直す

毎日の自動更新は GitHub Actions（.github/workflows/update-programs.yml）で動かし、
できた JSON を GitHub Pages で公開する。アプリはそれを1日1回読み込む。

負荷を抑えるため、演者の公式リンクは前回の JSON（--previous）にあるものを使い回し、
新しい演者の分と、毎週日曜（JST）だけ確認し直す。ある寄席の取得に失敗したときは、
その寄席だけ前回のデータを残す（「番組未発表」で上書きしない）。

取得したページは ~/Library/Caches/YoseKawaraban に保存し、2回目以降は再利用する
（--refresh で取り直し）。公式リンクは開けることを確かめたものだけを載せる。
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "ProgramData.json"
CACHE = Path.home() / "Library" / "Caches" / "YoseKawaraban"
UA = "Mozilla/5.0 (Macintosh) YoseKawaraban/0.1 (personal use)"

RK, GK = "rakugo-kyokai", "geikyo"
NOW = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).replace(microsecond=0)
TODAY = NOW.date()

args = None


# MARK: - 取得

def fetch(url, *, cache=True):
    key = hashlib.sha1(url.encode()).hexdigest()
    path = CACHE / key
    if cache and path.exists() and not args.refresh:
        return path.read_bytes().decode("utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    text = None
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    text = text or raw.decode("utf-8", "ignore")
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    time.sleep(0.4)  # 相手先に負担をかけない
    return text


def link_alive(url):
    key = "alive-" + hashlib.sha1(url.encode()).hexdigest()
    path = CACHE / key
    if path.exists() and not args.refresh:
        return path.read_text() == "1"
    ok = False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            ok = 200 <= r.status < 400
    except Exception:
        ok = False
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text("1" if ok else "0")
    time.sleep(0.2)
    return ok


def text_of(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


# MARK: - 名前の正規化

def squash(s):
    """空白を除いた表示用の名前"""
    s = unicodedata.normalize("NFKC", html.unescape(s))
    s = re.sub(r"\s+", "", s)
    # カタカナの後の長音もどき（－ - ―）を「ー」に
    s = re.sub(r"(?<=[゠-ヿ぀-ゟ])[-－―−]", "ー", s)
    return s


def match_key(name):
    """照合用：ひらがな→カタカナ、記号ゆれをそろえる"""
    s = squash(name)
    s = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
    for a, b in (("圓", "円"), ("齋", "斎"), ("斉", "斎"), ("髙", "高"), ("檜", "桧"), ("・", ""), ("&", ""), ("＆", "")):
        s = s.replace(a, b)
    return s


MEMBERS = None


def join_duo(names):
    """「翁家勝丸」「翁家丸果」→「翁家勝丸・丸果」。共通部分の長さは名簿に合うものを選ぶ"""
    names = [squash(n) for n in names if squash(n)]
    if len(names) < 2:
        return names[0] if names else ""
    first = names[0]
    common = 0
    while all(common < len(n) - 1 and n[common] == first[common] for n in names[1:]) and common < len(first) - 1:
        common += 1

    def build(k):
        return "・".join([first] + [n[k:] if k >= 2 else n for n in names[1:]])

    for k in range(common, 1, -1):
        if MEMBERS and MEMBERS.lookup(build(k)):
            return build(k)
    if MEMBERS and MEMBERS.lookup("・".join(names)):
        return "・".join(names)
    # 名簿にないとき：後ろの名前が2文字以上残る範囲で、最長3文字を共通部分（亭号）とみなす
    for k in range(min(common, 3), 1, -1):
        if all(len(n) - k >= 2 for n in names[1:]):
            return build(k)
    return "・".join(names)


# MARK: - 協会の芸人名簿

class Members:
    def __init__(self):
        self.by_key = {}      # match_key -> member
        self.by_url = {}

    def add(self, name, assoc, url, category, kana=""):
        m = {"name": squash(name), "assoc": assoc, "url": url, "category": category, "kana": kana}
        self.by_key.setdefault(match_key(name), m)
        self.by_url[url] = m

    def lookup(self, name, url=None):
        if url and url in self.by_url:
            return self.by_url[url]
        return self.by_key.get(match_key(name))

    def lookup_given(self, given, assoc):
        """名前の下だけ（「鯉三郎」）から探す。1人に決まるときだけ返す"""
        hits = [m for m in self.by_key.values() if m["assoc"] == assoc and m["name"].endswith(squash(given))]
        return hits[0] if len(hits) == 1 else None


def load_members():
    ms = Members()
    # 落語協会：真打（複数ページ）・二ツ目・前座・講談・色物
    pages = [("https://www.rakugo-kyokai.jp/members", "rakugo")]
    pages += [(f"https://www.rakugo-kyokai.jp/members?p={p}", "rakugo") for p in range(2, 6)]
    for code, cat in [("f", "rakugo"), ("z", "rakugo"), ("k", "kodan"), ("i", "iromono")]:
        pages.append((f"https://www.rakugo-kyokai.jp/members/{code}", cat))
        pages += [(f"https://www.rakugo-kyokai.jp/members/{code}?p={p}", cat) for p in range(2, 4)]
    for url, cat in pages:
        try:
            s = fetch(url)
        except Exception:
            continue
        for m in re.finditer(r'href="(/members/[A-Za-z0-9_-]{6,})">(.*?)</a>', s, re.S):
            parts = re.findall(r"<ruby>([^<]+)<rt>([^<]*)</rt>", m.group(2))
            if parts and plausible_name("".join(p for p, _ in parts)):
                ms.add("".join(p for p, _ in parts), RK, "https://www.rakugo-kyokai.jp" + m.group(1), cat,
                       kana="".join(k for _, k in parts))
    # 落語芸術協会
    # 講談・浪曲・色物を先に読む（真打の一覧にも載っている人がいるため、専門の区分を優先）
    gk_cat = {"8": "kodan", "9": "rokyoku", "5": "iromono", "1": "rakugo", "2": "rakugo", "3": "rakugo", "4": "rakugo",
              "0": None, "6": None}
    for disp, cat in gk_cat.items():
        try:
            s = fetch(f"https://www.geikyo.com/profile/index?disp={disp}")
        except Exception as e:
            print(f"  落語芸術協会の名簿 disp={disp}: 取得できません（{e!r}）", file=sys.stderr)
            continue
        for m in re.finditer(r'href="(https://www\.geikyo\.com/profile/detail\?id=\d+)"[^>]*>(.*?)</a>', s, re.S):
            name = squash(text_of(m.group(2)))
            # ふりがな・芸種は同じ行の次のセル。「塙 宣之（ナイツ）」の括弧内のグループには付けない
            in_group = "（" in s[max(0, m.start() - 80):m.start()].split("<td")[-1]
            after = s[m.end():m.end() + 600].split("</tr>")[0]
            cells = [squash(text_of(c)) for c in re.findall(r"<td[^>]*>([^<]*)</td>", after)]
            kana = cells[0] if cells and not in_group else ""
            if not re.fullmatch(r"[\u3040-\u309Fー・]+", kana):
                kana = ""
            genre = cells[1] if len(cells) > 1 else ""
            if name and plausible_name(name) and m.group(1) not in ms.by_url:
                # 名簿の芸種の列（講談・落語・漫才など）で分類する。真打の一覧には講談師も載っているため
                ms.add(name, GK, m.group(1), category_of(genre) or cat, kana=kana)
                ms.by_url[m.group(1)]["genre"] = genre
    return ms


# MARK: - 番組の内部表現

class Entry:
    """1人（または1組）の出演者"""

    def __init__(self, name, url=None, days=None):
        self.name = squash(name)
        self.url = url
        self.days = set(days) if days else None   # 出演する日（None = 全日）
        self.absent = set()                       # 休演の日
        self.subs = {}                            # 日 -> 代演者の名前


class Act:
    def __init__(self, genre, entries, alternating=False, tori=False):
        self.genre = squash(genre)
        self.entries = entries
        self.alternating = alternating
        self.tori = tori


class Program:
    def __init__(self, venue, slot, year, month, first, last, title, source_name, source_url, acts,
                 status="complete", note=None):
        self.venue, self.slot = venue, slot
        self.dates = [dt.date(year, month, d) for d in range(first, last + 1)]
        self.title, self.source_name, self.source_url = title, source_name, source_url
        self.acts, self.status, self.note = acts, status, note


def days_in(text):
    return [int(x) for x in re.findall(r"\d+", unicodedata.normalize("NFKC", text))]


def zen_num(s):
    return unicodedata.normalize("NFKC", s)


# MARK: - 鈴本演芸場

MARKERS = "┌｜│└"


def label_split(line):
    """先頭の5文字幅（半角は0.5）を芸種欄として切り出す"""
    width, i = 0.0, 0
    while i < len(line) and width < 5:
        width += 0.5 if unicodedata.east_asian_width(line[i]) in ("H", "Na") else 1
        i += 1
    label, rest = line[:i], line[i:]
    if not rest:
        return None
    marker = rest[0] if rest[0] in MARKERS else ""
    if not marker and rest[0] not in "　 ":
        return None
    return squash(label), marker, rest[1:]


def plausible_name(name):
    if set(name) <= set("ー－-―─‐・ 　"):
        return False  # 名簿の区切り線など
    if not 1 <= len(name) <= 14 or re.search(r"[0-9０-９（）()『』「」：:【】]", name):
        return False
    return not any(w in name for w in ("口上", "番組", "前座", "仲入", "お囃子"))


def parse_suzumoto_page(url):
    s = fetch(url)
    s = s.replace("<鈴本余一会>", "＜鈴本余一会＞")
    h3 = re.search(r"<h3>(.*?)</h3>", s, re.S)
    body = re.search(r'<div id="update1">(.*?)</div>', s, re.S)
    if not h3 or not body:
        return None
    head = zen_num(text_of(h3.group(1)))
    m = re.search(r"(\d+)月(上席|中席|下席)?.*?[（(](\d+)日[～〜~-](\d+)日[)）]", head)
    yoichi = re.search(r"(\d+)月(\d+)日(昼|夜)の部", head)
    lines = text_of(re.sub(r"<br\s*/?>", "\n", body.group(1), flags=re.I)).split("\n")

    acts, group = [], None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if re.search(r"(前\s*座|笛|お囃子)\s*：", line):
            break  # 余一会の末尾（前座・お囃子の一覧）
        sub = re.match(r"^\s*\[(\d+)日\s*(.+?)\s*代演\s*\]\s*$", line)
        if sub and acts:
            acts[-1].entries[-1].subs[int(sub.group(1))] = squash(sub.group(2))
            continue
        bracket = re.search(r"\[([^\]]*)\]\s*$", line)
        info = bracket.group(1) if bracket else ""
        if bracket:
            line = line[:bracket.start()]
        parts = label_split(line)
        if not parts:
            continue
        label, marker, rest = parts
        name = squash(rest)
        if not plausible_name(name) or label in ("開口一番",):
            continue
        entry = Entry(name)
        if "出演" in info:
            entry.days = set(days_in(info))
        elif "休演" in info:
            entry.absent = set(days_in(info))

        if marker == "┌" and label and label != "交代出演":
            group = Act(label, [entry])
            acts.append(group)
        elif group and (label == "交代出演" or (label and marker in "｜│└" and marker)):
            group.alternating = True
            group.entries.append(entry)
        elif group and not label and marker in "｜│└" and marker:
            last = group.entries[-1]
            if getattr(last, "_opened", False) or (len(group.entries) == 1 and not group.alternating):
                # 2人組：直前の行とつなげる
                merged = Entry(join_duo([last.name, entry.name]))
                merged.days, merged.absent = entry.days or last.days, entry.absent or last.absent
                merged._opened = getattr(last, "_opened", False)
                group.entries[-1] = merged
                continue
            group.alternating = True
            group.entries.append(entry)
        else:
            group = Act(label or (acts[-1].genre if acts else "落語"), [entry])
            acts.append(group)
            group = None if not marker else group
        entry._opened = marker == "┌" and label == "交代出演"

    if not acts:
        return None
    acts[-1].tori = True
    slot = "night" if ("yoru" in url or (yoichi and yoichi.group(3) == "夜")) else "day"
    if m:
        month, first, last = int(m.group(1)), int(m.group(3)), int(m.group(4))
        title = f"{month}月{m.group(2) or ''} {'夜' if slot == 'night' else '昼'}の部"
    elif yoichi:
        month, first = int(yoichi.group(1)), int(yoichi.group(2))
        last = first
        special = re.search(r"＜鈴本余一会＞\s*(.+)$", head) or re.search(r"<鈴本余一会>\s*(.+)$", head)
        title = "余一会 " + (squash(special.group(1)) if special else "")
    else:
        return None
    year = TODAY.year + (1 if month < TODAY.month - 6 else 0)
    return Program("suzumoto", slot, year, month, first, last, title.strip(), "鈴本演芸場 公式サイト", url, acts)


def suzumoto_programs():
    top = fetch("https://rakugo.or.jp/")
    pages = sorted(set(re.findall(r'href="(\d{4}-\d{1,2}(?:kami|naka|shimo)-(?:hiru|yoru)\.html|\d{4}-\d{1,2}yoichi\.html)"', top)))
    out = []
    for p in pages:
        try:
            prog = parse_suzumoto_page("https://rakugo.or.jp/" + p)
        except Exception as e:
            print(f"  鈴本 {p}: {e!r}", file=sys.stderr)
            continue
        if prog:
            out.append(prog)
    return out


# MARK: - 新宿末廣亭

def suehirotei_programs():
    base = "http://suehirotei.com/"
    top = fetch(base)
    pages = sorted(set(re.findall(r'href="(\d{1,2}(?:kami|naka|shimo)\.html)"', top)))
    out = []
    for p in pages:
        url = base + p
        s = fetch(url)
        main = s[s.find("<main"):]
        head = zen_num(text_of(re.search(r'class="[^"]*jouseki[^"]*">(.*?)</div>', main, re.S).group(1)))
        m = re.search(r"(\d+)月(上席|中席|下席)\((\d+)日[～〜~](\d+)日\)", head)
        if not m:
            continue
        month, first, last = int(m.group(1)), int(m.group(3)), int(m.group(4))
        year = TODAY.year + (1 if month < TODAY.month - 6 else 0)
        for part in re.split(r'<th colspan="4"', main)[1:]:
            slot = "day" if "昼の部" in part[:200] else "night"
            acts, group = [], None
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", part, re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if len(cells) < 3:
                    continue
                marker = squash(text_of(cells[1]))
                cell = cells[2]
                genre = squash(text_of(cells[3])) if len(cells) > 3 else ""
                dates = re.search(r'class="futatsume">\s*([\d,，\s]+日)\s*<', cell)
                link = re.search(r'href="([^"]+)"', cell)
                name_html = re.sub(r'<span class="grad-wrap">.*?</span>\s*</span>', "", cell, flags=re.S)
                name_html = re.sub(r'<span class="futatsume">.*?</span>', "", name_html, flags=re.S)
                name = squash(text_of(re.sub(r'<span class="text-danger">.*?</span>', "", name_html)))
                name = re.sub(r"^(壽)?(二ツ目昇進|真打昇進)", "", name)
                if not name or "仲入" in name:
                    continue
                entry = Entry(name, link.group(1) if link else None,
                              days_in(dates.group(1)) if dates else (days_in(marker) if marker.endswith("日") else None))
                if marker == "交互":
                    group = Act(genre, [entry], alternating=True)
                    acts.append(group)
                elif marker == "〃" and group:
                    group.entries.append(entry)
                else:
                    group = None
                    acts.append(Act(genre, [entry], tori=(marker == "主任")))
            if acts:
                if not any(a.tori for a in acts):
                    acts[-1].tori = True
                title = f"{month}月{m.group(2)} {'昼' if slot == 'day' else '夜'}の部"
                out.append(Program("suehirotei", slot, year, month, first, last, title, "新宿末廣亭 公式サイト", url, acts))
    return out


# MARK: - 浅草演芸ホール（日付ごと）

def asakusa_programs():
    base = "https://www.asakusaengei.com/schedule/"
    top = fetch(base)
    cas = []
    for c in re.findall(r'href="\?ca=(\d+)"', top):
        if c not in cas:
            cas.append(c)
    out = []
    for c in cas:
        url = f"{base}?ca={c}"
        s = fetch(url)
        dm = re.search(r"＞\s*(\d{4})年(\d{1,2})月(\d{1,2})日", s)
        if not dm:
            continue
        y, mo, d = map(int, dm.groups())
        if dt.date(y, mo, d) < TODAY:
            continue
        for part in re.split(r'<li class="f\d">', s)[1:]:
            slot = "day" if "昼の部" in part[:300] else "night"
            acts = []
            for row in re.findall(r"<tr><th><p>(.*?)</p></th><td>(.*?)</table></td></tr>", part, re.S):
                genre, td = row
                tori = "主任" in td.split("<table")[0]
                inner = td[td.find("<table"):]
                cells = [c for c in re.findall(r"<td[^>]*>(.*?)</td>", inner, re.S)]
                alternating = "（交互）" in inner
                texts = [c for c in cells if "<img" not in c and "交互" not in c]
                names = []
                for c in texts:
                    parts = [squash(text_of(x)) for x in re.split(r"<br\s*/?>", c)]
                    names.append([p for p in parts if p])
                names = [n for n in names if n]
                if not names or any("仲入" in x for n in names for x in n) or any("口上" in x for n in names for x in n):
                    continue
                if alternating:
                    flat = [x for n in names for x in n]
                    entries = [Entry(x) for x in flat]
                elif len(names) == 2 and len(names[0]) == 1 and len(names[1]) >= 2:
                    # 「すず風」「にゃん子<br>金魚」のような組
                    family = names[0][0]
                    entries = [Entry(join_duo([family + names[1][0]] + [family + x for x in names[1][1:]]))]
                else:
                    flat = [x for n in names for x in n]
                    entries = [Entry(join_duo(flat) if len(flat) > 1 else flat[0])]
                acts.append(Act(squash(text_of(genre)), entries, alternating=alternating, tori=tori))
            if acts:
                if not any(a.tori for a in acts):
                    acts[-1].tori = True
                sec = "上席" if d <= 10 else "中席" if d <= 20 else "下席"
                title = f"{mo}月{sec} {'昼' if slot == 'day' else '夜'}の部"
                out.append(Program("asakusa", slot, y, mo, d, d, title, "浅草演芸ホール 公式サイト", url, acts))
    return out


# MARK: - 池袋演芸場（落語協会）

def rk_ikebukuro_programs():
    base = "https://www.rakugo-kyokai.jp/joseki/ikebukuro"
    first_page = fetch(base)
    titles = []
    for t in re.findall(r'role="option"[^>]*>([^<]+)<', first_page):
        if t not in titles:
            titles.append(t)
    out = []
    for t in titles:
        url = base + "?title=" + urllib.parse.quote(t)
        s = fetch(url)
        s = re.sub(r"<script.*?</script>|<svg.*?</svg>", "", s, flags=re.S)
        head = re.search(r'id="program" aria-label="池袋演芸場 (\d+)月(上席|中席|下席)\s*（(\d+)日〜(\d+)日）"', s)
        if not head:
            continue
        month, first, last = int(head.group(1)), int(head.group(3)), int(head.group(4))
        year = TODAY.year + (1 if month < TODAY.month - 6 else 0)
        # 番組は隠し要素（S:0）に入っていることがあるので、最後に出てくるものを使う
        region = s[s.rfind('aria-labelledby="ikebukuro"'):]
        for block in re.split(r"<p[^>]*>(?=昼席|夜席)", region)[1:]:
            slot = "day" if block.startswith("昼席") else "night"
            title = f"{month}月{head.group(2)} {'昼' if slot == 'day' else '夜'}の部"
            acts = []
            special = []
            lis = []
            for ul in re.findall(r"<ul[^>]*>(.*?)</ul>", block, re.S):
                head_li = re.search(r"<li[^>]*>\s*<span[^>]*>\s*<span[^>]*>([^<]*)<", ul)
                if head_li and squash(head_li.group(1)) in ("お囃子", "前座"):
                    continue  # お囃子・前座の一覧は番組に入れない
                lis += re.findall(r"<li[^>]*>(.*?)</li>", ul, re.S)
            for li in lis:
                if not li.strip():
                    continue
                alternating = ">交互<" in li
                links = re.findall(r'href="(/members/[^"]+)"[^>]*>(.*?)</a>', li, re.S)
                day_list = re.findall(r"<div[^>]*>\s*(\d+)日\s*</div>\s*<p[^>]*>(.*?)</p>", li, re.S)
                if day_list:
                    entries = [Entry(squash(text_of(n)), days=[int(d)]) for d, n in day_list]
                    acts.append(Act("", entries, alternating=True))
                    continue
                if links:
                    entries = []
                    for href, inner in links:
                        nm = squash(text_of(re.sub(r"<img[^>]*>", "", inner)))
                        entries.append(Entry(nm, "https://www.rakugo-kyokai.jp" + href))
                    tori = "<img" in li
                    acts.append(Act("", entries, alternating=alternating and len(entries) > 1, tori=tori))
                    continue
                txt = squash(text_of(li))
                if txt and "仲入" not in txt and "口上" not in txt and not re.match(r"^\d+:\d+$", txt):
                    special.append(re.sub(r"^\d+:\d+", "", txt))
            status, note = "complete", None
            if not acts:
                # 夜席が特選会（独演会など）の場合：出演者の一覧はない
                status, note = "partial", "落語協会特選会（日替わりの会。出演者は会ごとに異なります）"
            elif not any(a.tori for a in acts):
                acts[-1].tori = True
            out.append(Program("ikebukuro", slot, year, month, first, last, title, "落語協会 定席番組", url, acts,
                               status=status, note=note))
    return out


# MARK: - 池袋演芸場（落語芸術協会）

def parse_alternation_notes(text):
    """「二ツ目交互 11 鯉白 12 信楽 …」→ [[(days, 名前), …], …]"""
    text = zen_num(text)
    segments = []
    for seg in re.split(r"(?:\d{1,2}:\d{2})?[^\s\d]*交互", text)[1:]:
        pairs = re.findall(r"(\d+(?:[.・,]\d+)*)\s+([^\s\d][^\s]*)", seg)
        if pairs:
            segments.append([([int(x) for x in re.split(r"[.・,]", d)], n) for d, n in pairs])
    return segments


def gk_ikebukuro_programs(members):
    idx = fetch("https://www.geikyo.com/schedule/index")
    ids = sorted(set(re.findall(r"jyoseki_detail\?id=(\d+)", idx)))
    out = []
    for i in ids:
        url = f"https://www.geikyo.com/schedule/jyoseki_detail?id={i}"
        s = fetch(url)
        t = re.sub(r"\s+", " ", text_of(s))
        if "池袋演芸場" not in t[:3000] and "池袋演芸場" not in t:
            continue
        hm = re.search(r"([十一二三]+)月([上中下])席\s*(前半|後半)?\s*(\d+)月(\d+)〜(\d+)日", t)
        if not hm or "池袋演芸場" not in t[max(0, hm.start() - 200):hm.end() + 50]:
            continue
        month, first, last = int(hm.group(4)), int(hm.group(5)), int(hm.group(6))
        year = TODAY.year + (1 if month < TODAY.month - 6 else 0)
        for table in re.findall(r'<table cellpadding="5px".*?</table>', s, re.S):
            rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
            if not rows:
                continue
            head = squash(text_of(rows[0]))
            slot = "day" if head.startswith("昼の部") else "night"
            subtitle = head.replace("昼の部", "").replace("夜の部", "")
            acts, group, notes = [], None, []
            for row in rows[1:]:
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if len(cells) < 3:
                    continue
                marker = squash(text_of(cells[1]))
                link = re.search(r'href="([^"]+)"', cells[2])
                left = re.search(r'class="LeftItem">(.*?)</p>', cells[2], re.S)
                right = re.search(r'class="RightItem">(.*?)</p>', cells[2], re.S)
                if not left:
                    txt = squash(text_of(cells[2]))
                    if txt and "仲入" not in txt:
                        notes.append(re.sub(r"\s+", " ", text_of(cells[2])).strip())
                    continue
                name = squash(text_of(left.group(1)))
                inline = re.search(r"[〔［\[]([^〕］\]]+)[〕］\]]$", name)
                if inline:
                    name = name[:inline.start()]
                genre = re.sub(r"[［］〔〕\[\]]", "", squash(text_of(right.group(1)))) if right else ""
                genre = genre or (inline.group(1) if inline else "")
                entry = Entry(name, link.group(1) if link else None)
                if marker.startswith("┌"):
                    group = Act(genre, [entry], alternating=True)
                    acts.append(group)
                elif marker and marker[0] in "｜└|" and group:
                    group.entries.append(entry)
                else:
                    group = None
                    acts.append(Act(genre, [entry]))
            if not acts:
                continue
            acts[-1].tori = True
            # 備考の交互日程を、交互のグループに順番に当てはめる
            segments = []
            for n in notes:
                segments += parse_alternation_notes(n)
            used = set()
            for act in [a for a in acts if a.alternating]:
                for si, seg in enumerate(segments):
                    if si in used:
                        continue
                    givens = [n.split("→")[0] for _, n in seg]
                    if all(any(e.name.endswith(squash(g)) for e in act.entries) for g in givens):
                        used.add(si)
                        for e in act.entries:
                            e.days = set()
                        for days, n in seg:
                            orig, _, repl = n.partition("→")
                            e = next(e for e in act.entries if e.name.endswith(squash(orig)))
                            for d in days:
                                e.days.add(d)
                                if repl:
                                    m = members.lookup_given(repl, GK)
                                    e.subs[d] = m["name"] if m else squash(repl)
                        break
            title = f"{month}月{hm.group(2)}席 {'昼' if slot == 'day' else '夜'}の部"
            if subtitle:
                title += " " + subtitle
            out.append(Program("ikebukuro", slot, year, month, first, last, title, "落語芸術協会 公演スケジュール",
                               url, acts))
    return out


# MARK: - JSON に組み立てる

def category_of(genre):
    g = genre or ""
    if "講談" in g:
        return "kodan"
    if "浪曲" in g:
        return "rokyoku"
    if "落語" in g:
        return "rakugo"
    return "iromono" if g else None


SNS = {"x.com": "X", "twitter.com": "X", "facebook.com": "Facebook", "instagram.com": "Instagram",
       "youtube.com": "YouTube", "tiktok.com": "TikTok", "threads.net": "Threads", "note.com": "note"}
GENERIC = re.compile(r"maps\.app\.goo\.gl|twitter\.com/rakugokyokai|x\.com/rakugokyokai|UCUc7tai_P55ZPGl1m2RcwOg|"
                     r"rakugo-kyokai\.jp|geikyo\.com|google|gstatic|w3\.org|schema\.org|/share|sharer|line\.me|"
                     r"jquery|cdn|fonts\.")


def official_links(profile_url, known=()):
    """協会プロフィールに載っている公式サイト・SNS。開けることを確かめたものだけ。
    以前に確認できたリンク（known）は、今回開けなくても残す（GitHub のサーバーは海外なので、
    海外からの接続を受け付けないサイトがある）"""
    known = {x["url"]: x for x in known}
    if args.no_links:
        return list(known.values())
    try:
        s = fetch(profile_url)
    except Exception:
        return []
    links = []
    for u in re.findall(r'href="(https?://[^"\s]+)\s*"', s):
        u = u.strip()
        if GENERIC.search(u) or u in links:
            continue
        links.append(u)
    out = []
    for u in links:
        host = urllib.parse.urlparse(u).netloc.lower().removeprefix("www.")
        sns = next((v for k, v in SNS.items() if host.endswith(k)), None)
        if not link_alive(u):
            if u in known:
                out.append(known[u])
            continue
        if sns:
            out.append({"kind": "sns", "title": sns, "url": u, "checkedAt": TODAY.isoformat()})
        else:
            out.append({"kind": "officialSite", "title": f"ホームページ（{host}）", "url": u,
                        "checkedAt": TODAY.isoformat()})
    return out


def build(programs, members, previous=None, previous_list=()):
    venues = [
        {"id": "asakusa", "name": "浅草演芸ホール", "shortName": "浅草", "kana": "あさくさえんげいほーる", "area": "浅草",
         "sources": [{"kind": "officialSite", "title": "浅草演芸ホール 公式サイト", "url": "https://www.asakusaengei.com/",
                      "checkedAt": TODAY.isoformat()}]},
        {"id": "suzumoto", "name": "鈴本演芸場", "shortName": "鈴本", "kana": "すずもとえんげいじょう", "area": "上野",
         "sources": [{"kind": "officialSite", "title": "鈴本演芸場 公式サイト", "url": "https://rakugo.or.jp/",
                      "checkedAt": TODAY.isoformat()}]},
        {"id": "suehirotei", "name": "新宿末廣亭", "shortName": "末廣亭", "kana": "しんじゅくすえひろてい", "area": "新宿",
         "sources": [{"kind": "officialSite", "title": "新宿末廣亭 公式サイト", "url": "http://suehirotei.com/",
                      "checkedAt": TODAY.isoformat()}]},
        {"id": "ikebukuro", "name": "池袋演芸場", "shortName": "池袋", "kana": "いけぶくろえんげいじょう", "area": "池袋",
         "sources": [{"kind": "association", "title": "落語協会 定席番組（池袋演芸場）",
                      "url": "https://www.rakugo-kyokai.jp/joseki/ikebukuro", "checkedAt": TODAY.isoformat()}]},
    ]
    associations = [
        {"id": RK, "name": "落語協会", "url": "https://www.rakugo-kyokai.jp/"},
        {"id": GK, "name": "落語芸術協会", "url": "https://www.geikyo.com/"},
    ]

    # 演者を集める（名前を名簿の表記にそろえる）
    performers = {}
    genres = defaultdict(Counter)

    def canon(entry_name, url=None):
        m = members.lookup(entry_name, url)
        name = m["name"] if m else squash(entry_name)
        if name not in performers:
            performers[name] = {"member": m, "url": url}
        elif url and not performers[name]["url"]:
            performers[name]["url"] = url
        return name

    for p in programs:
        for a in p.acts:
            if any("未定" in e.name for e in a.entries):
                p.status, p.note = "partial", p.note or "出演者未定の枠あり"
            a.entries = [e for e in a.entries if plausible_name(e.name) and "未定" not in e.name]
        p.acts = [a for a in p.acts if a.entries]
        if p.acts and not any(a.tori for a in p.acts):
            p.acts[-1].tori = True
        for a in p.acts:
            for e in a.entries:
                e.name = canon(e.name, e.url)
                if a.genre:
                    genres[e.name][a.genre] += 1
                e.subs = {d: canon(n) for d, n in e.subs.items()}

    # 公演（日ごと）
    performances = []
    fetched = NOW.isoformat()
    covered = set()
    apid = 0
    for p in programs:
        assoc_votes = Counter(performers[e.name]["member"]["assoc"] for a in p.acts for e in a.entries
                              if performers[e.name]["member"])
        assoc = assoc_votes.most_common(1)[0][0] if assoc_votes else None
        for day in p.dates:
            if day < TODAY:
                continue
            apps = []
            for order, a in enumerate(p.acts, start=1):
                known = [e for e in a.entries if e.days is not None]
                todays = [e for e in known if day.day in e.days]
                for e in a.entries:
                    def add(pid, status, **kw):
                        nonlocal apid
                        apid += 1
                        item = {"id": f"a{apid}", "performerID": pid, "order": order, "isTori": a.tori,
                                "status": status, "confirmation": "confirmed", "sourceName": p.source_name,
                                "sourceURL": p.source_url, "fetchedAt": fetched}
                        item.update({k: v for k, v in kw.items() if v})
                        apps.append(item)

                    others = [x.name for x in a.entries if x is not e]
                    if a.alternating:
                        if e.days is not None:
                            if day.day not in e.days:
                                continue
                            status = "scheduled"
                        elif todays:
                            continue  # この日は日付の決まっている別の人が出る
                        else:
                            status = "alternating"
                    else:
                        if e.days is not None and day.day not in e.days:
                            continue
                        status = "scheduled"
                    partner = others[0] if (a.alternating and others) else None
                    if day.day in e.subs:
                        add(e.name, "absent")
                        add(e.subs[day.day], "substitute", substituteFor=e.name)
                    elif day.day in e.absent:
                        add(e.name, "absent", note="休演")
                    else:
                        note = "交互出演（本日出演）" if (a.alternating and status == "scheduled") else None
                        add(e.name, status, alternatingWith=partner, note=note)
            key = (p.venue, day, p.slot)
            covered.add(key)
            performances.append({
                "id": f"{p.venue}-{day.isoformat()}-{p.slot}", "venueID": p.venue, "date": day.isoformat(),
                "slot": p.slot, "section": section_of(day), "title": p.title, "associationID": assoc,
                "informationStatus": p.status, "note": p.note, "appearances": apps,
            })

    # 番組が出ていない日は「番組未発表」として入れる（出演なしと誤判定しないため）
    if performances:
        start = TODAY
        end = max(dt.date.fromisoformat(x["date"]) for x in performances)
        d = start
        while d <= end:
            for v in venues:
                for slot in ("day", "night"):
                    if (v["id"], d, slot) not in covered:
                        performances.append({
                            "id": f"{v['id']}-{d.isoformat()}-{slot}", "venueID": v["id"], "date": d.isoformat(),
                            "slot": slot, "section": section_of(d), "title": f"{d.month}月 {section_name(d)}",
                            "associationID": None, "informationStatus": "unannounced", "note": "番組未発表",
                            "appearances": [],
                        })
            d += dt.timedelta(days=1)
    performances.sort(key=lambda x: (x["date"], x["venueID"], x["slot"]))
    # 同じ id の重複（同じ日を2つの番組が含む場合）は先のものを残す
    seen, unique = set(), []
    for x in performances:
        if x["id"] not in seen:
            seen.add(x["id"])
            unique.append(x)
    performances = unique

    # 演者の情報（公式リンクは前回の確認結果を使い回す。日曜と --refresh-links のときは確認し直す）
    previous_links, links_checked = {}, {}
    for prev in previous_list:
        for pp in prev.get("performers", []):
            if pp.get("linksCheckedAt"):
                links_checked[pp["id"]] = max(links_checked.get(pp["id"], ""), pp["linksCheckedAt"])
            merged = {x["url"]: x for x in previous_links.get(pp["id"], [])}
            for x in pp.get("sources", []):
                if x.get("kind") != "association":
                    merged.setdefault(x["url"], x)
            previous_links[pp["id"]] = list(merged.values())
    recheck_links = args.refresh_links or TODAY.weekday() == 6
    out_performers = []
    for i, (name, info) in enumerate(sorted(performers.items())):
        m = info["member"]
        genre = genres[name].most_common(1)[0][0] if genres[name] else None
        # 分類は協会の名簿の区分を優先（番組によって芸種の書き方がまちまちなため）
        member_cat = m and m["category"]
        cat = member_cat or category_of(genre) or "rakugo"
        if genre and category_of(genre) != cat:
            genre = None  # 名簿の区分と合わない芸種表記は使わない
        assoc = m["assoc"] if m else None
        profile_url = (m and m["url"]) or info["url"]
        sources = []
        if profile_url:
            if name in previous_links and not recheck_links:
                sources += previous_links[name]
            else:
                sources += official_links(profile_url, previous_links.get(name, []))
                links_checked[name] = TODAY.isoformat()
            aname = "落語協会" if "rakugo-kyokai" in profile_url else "落語芸術協会" if "geikyo" in profile_url else "協会"
            sources.append({"kind": "association", "title": f"{aname} プロフィール", "url": profile_url,
                            "checkedAt": TODAY.isoformat()})
        assoc_name = {RK: "落語協会", GK: "落語芸術協会"}.get(assoc)
        member_genre = m.get("genre") if (m and category_of(m.get("genre") or "") == cat) else None
        genre_label = genre or member_genre or {"rakugo": "落語", "kodan": "講談", "rokyoku": "浪曲", "iromono": "色物"}[cat]
        profile = (f"{assoc_name}所属。" if assoc_name else "") + f"芸種は{genre_label}。"
        out_performers.append({
            "id": name, "name": name, "kana": (m and m.get("kana")) or "", "category": cat, "genre": genre_label,
            "associationID": assoc, "profile": profile, "sources": sources,
            "linksCheckedAt": links_checked.get(name),
        })
        if (i + 1) % 50 == 0:
            print(f"  演者 {i + 1}/{len(performers)}", file=sys.stderr)

    # 番組に出ない人も、両協会の名簿にいれば一覧に入れる（先に好き・苦手を登録できるように）。
    # 負担を抑えるため、公式リンクは前回までに確かめた結果を使い、まだの人は1回に --member-links 人まで確かめる。
    listed = {p["id"] for p in out_performers}
    label = {"rakugo": "落語", "kodan": "講談", "rokyoku": "浪曲", "iromono": "色物"}
    budget = args.member_links  # まだ確かめていない人を1回にこの人数まで
    for m in sorted(members.by_url.values(), key=lambda m: m["name"]):
        name = m["name"]
        if name in listed or not plausible_name(name):
            continue
        listed.add(name)
        cat = m["category"] or "rakugo"
        genre = m.get("genre") if m.get("genre") and category_of(m["genre"]) == cat else label[cat]
        assoc_name = {RK: "落語協会", GK: "落語芸術協会"}[m["assoc"]]
        links = previous_links.get(name, [])
        if name not in links_checked and budget > 0:
            budget -= 1
            links = official_links(m["url"], links)
            links_checked[name] = TODAY.isoformat()
        out_performers.append({
            "id": name, "name": name, "kana": m.get("kana") or "", "category": cat, "genre": genre,
            "associationID": m["assoc"], "profile": f"{assoc_name}所属。芸種は{genre}。",
            "sources": links + [{"kind": "association", "title": f"{assoc_name} プロフィール",
                                 "url": m["url"], "checkedAt": TODAY.isoformat()}],
            "linksCheckedAt": links_checked.get(name),
        })
    out_performers.sort(key=lambda p: p["id"])

    dates = [x["date"] for x in performances]
    return {
        "meta": {
            "notice": f"番組は各寄席の公式サイトと落語協会・落語芸術協会のサイトから {TODAY.month}月{TODAY.day}日に取得しました。"
                      "出演者は変更されることがあります。お出かけ前に公式サイトでご確認ください。",
            "generatedAt": fetched,
            "coverage": {"start": min(dates), "end": max(dates)},
        },
        "associations": associations,
        "venues": venues,
        "performers": out_performers,
        "performances": performances,
    }


def keep_previous(data, previous, venues):
    """取得に失敗した寄席は、前回のデータ（今日以降の分）を残す"""
    today = TODAY.isoformat()
    old = {p["id"]: p for p in previous.get("performances", [])
           if p["venueID"] in venues and p["date"] >= today}
    if not old:
        return
    # 今回「番組未発表」になってしまった公演だけを前回の内容で埋める
    data["performances"] = [old.get(p["id"], p) if p["venueID"] in venues and p["informationStatus"] == "unannounced"
                            else p for p in data["performances"]]
    known = {p["id"] for p in data["performers"]}
    needed = {a["performerID"] for p in old.values() for a in p["appearances"]}
    for pp in previous.get("performers", []):
        if pp["id"] in needed and pp["id"] not in known:
            data["performers"].append(pp)
    names = "・".join(sorted(venues))
    print(f"前回のデータを残した寄席: {names}", file=sys.stderr)


def section_of(d):
    return "kami" if d.day <= 10 else "naka" if d.day <= 20 else "shimo" if d.day <= 30 else "yoichi"


def section_name(d):
    return {"kami": "上席", "naka": "中席", "shimo": "下席", "yoichi": "余一会"}[section_of(d)]


def main():
    global args
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="キャッシュを使わずに取り直す")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-links", action="store_true", help="公式リンクの取得・確認を省く（試験用）")
    ap.add_argument("--previous", action="append", default=[],
                    help="前回の ProgramData.json（公式リンクの使い回しと、取得失敗時の予備）。複数指定可、最初のものを予備に使う")
    ap.add_argument("--refresh-links", action="store_true", help="公式リンクをすべて確認し直す")
    ap.add_argument("--member-links", type=int, default=40,
                    help="番組に出ない演者の公式リンクを、まだ確かめていない人から1回にこの人数まで確かめる")
    args = ap.parse_args()
    previous_list = []
    for path in [Path(x) for x in args.previous] or [Path(args.out)]:
        if path.exists():
            try:
                previous_list.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
    previous = previous_list[0] if previous_list else None

    global MEMBERS
    print("協会の名簿を読み込み中…", file=sys.stderr)
    members = load_members()
    MEMBERS = members
    print(f"  {len(members.by_url)} 人", file=sys.stderr)
    programs, failed = [], set()
    for venue, name, fn in [("suzumoto", "鈴本演芸場", suzumoto_programs),
                            ("suehirotei", "新宿末廣亭", suehirotei_programs),
                            ("asakusa", "浅草演芸ホール", asakusa_programs),
                            ("ikebukuro", "池袋演芸場（落語協会）", rk_ikebukuro_programs),
                            ("ikebukuro", "池袋演芸場（落語芸術協会）", lambda: gk_ikebukuro_programs(members))]:
        try:
            got = fn()
            print(f"{name}: {len(got)} 番組", file=sys.stderr)
            if not got:
                failed.add(venue)
            programs += got
        except Exception as e:  # 1か所の失敗で全体を止めない
            print(f"{name}: 取得に失敗しました（{e!r}）", file=sys.stderr)
            failed.add(venue)
    if len(failed) == 4:
        print("すべての寄席で取得に失敗したため、書き出しません", file=sys.stderr)
        sys.exit(1)
    data = build(programs, members, previous, previous_list)
    if failed and previous:
        keep_previous(data, previous, failed)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    apps = sum(len(p["appearances"]) for p in data["performances"])
    print(f"書き出し: {args.out}（公演 {len(data['performances'])}、出演 {apps}、演者 {len(data['performers'])}）",
          file=sys.stderr)


if __name__ == "__main__":
    main()
