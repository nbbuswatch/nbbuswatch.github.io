"""Post the tracker's figures to the Facebook Page.

  python tools/post_social.py auto       -> run by the collector every 5 min; posts the afternoon summary once a day
  python tools/post_social.py summary    -> post the summary now
  python tools/post_social.py weekly     -> post last week's review now (Mondays 8am automatically)
  python tools/post_social.py story      -> post the morning story now (yesterday's result; 7am automatically)
  add --dry-run to only build the image and text (saved in data/social/)

One post a day at POST_AT (Sydney time): today so far, the last 7 days and the last 30 days, with a link to the site.
Needs the GitHub secrets FB_PAGE_ID and FB_PAGE_TOKEN; without them nothing is posted.
"""
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import card_styles  # noqa: E402  pool of 20 post image designs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
OUT_DIR = os.path.join(DATA, "social")
STATE = os.path.join(OUT_DIR, "posted.json")
SITE = "https://nbbuswatch.github.io/"
INSTALL = SITE + "install/"
SYD = ZoneInfo("Australia/Sydney")
FB_API = "https://graph.facebook.com/v26.0"
PAGE_ID = "1455302057670675"   # Northern Beaches Bus Watch (public)
POST_AT = (17, 30)       # 5:30 pm, mid evening peak while commuters are on the bus home
WEEKLY_AT = (8, 0)       # Mondays 8:00 am: week in review (Monday to Sunday just gone)
STORY_AT, STORY_UNTIL = (7, 0), 10   # morning story: yesterday's result, 7-10 am
POST_UNTIL, WEEKLY_UNTIL = 21, 12   # if the tracker was down, skip the post rather than send it late at night
MAX_TRIES = 3
LAST_ERROR = ""   # why the last Facebook post failed (token removed), saved in posted.json


def rows(path):
    with open(os.path.join(DATA, path), newline="") as f:
        return list(csv.DictReader(f))


def n(v):
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "–"


def period(daily, route_rows, dates):
    days = [r for r in daily if r["date"] in dates and n(r["scheduled"])]
    sched = sum(n(r["scheduled"]) for r in days)
    cxl = sum(n(r["cancelled"]) for r in days)
    obs = sum(n(r["observed"]) for r in days)
    on = sum(n(r.get("ontime")) for r in days)
    peak = {k: sum(n(r.get(k)) for r in days) for k in
            ("am_scheduled", "am_cancelled", "pm_scheduled", "pm_cancelled", "am_observed", "am_late", "pm_observed", "pm_late")}
    worst = Counter()
    for r in route_rows:
        if r["date"] in dates:
            worst[r["route"]] += n(r["cancelled"])
    top = worst.most_common(1)
    return {"days": len(days), "sched": sched, "cxl": cxl, "obs": obs, "on": on, "peak": peak,
            "worst": top[0] if top and top[0][1] else None,
            "first": min((r["date"] for r in days), default=None)}


# Disappointed opening line when the day is bad. Aimed at the service, never at drivers or a party.
BAD_CXL, VERY_BAD_CXL, BAD_ONTIME = 3.0, 6.0, 85.0   # % cancelled, % cancelled, % on time
BAD_LINES = [
    "😞 Another disappointing day for Beaches bus users.",
    "😞 Not good enough today. Commuters deserve better.",
    "😔 Plenty of people left waiting at stops today.",
    "😞 A rough day on the Beaches buses.",
    "😕 Not the service the Beaches should be getting.",
    "😞 Disappointing numbers again today.",
    "😔 Another day of waiting for buses that didn't come.",
    "😞 Today fell short. Again.",
]
VERY_BAD_LINES = [
    "😞 A really bad day for Beaches commuters.",
    "😔 Hard to sugar-coat this one. A really poor day.",
    "😞 A very poor day. Commuters deserve far better.",
    "😠 Not acceptable. Far too many buses let people down today.",
    "😞 One of those days where the bus just didn't turn up.",
    "😔 Grim numbers today for anyone relying on the bus.",
]
LATE_LINES = [
    "😞 Disappointing day: too many buses running late.",
    "😔 Lots of late running today. Not good enough.",
    "😕 If your bus was late today, you weren't alone.",
    "😞 Timetable? More of a suggestion today.",
]


WEEK_BAD_LINES = [
    "😞 Another disappointing week for Beaches bus users.",
    "😞 Not good enough last week. Commuters deserve better.",
    "😔 A week of too many no-shows at Beaches bus stops.",
    "😕 Not the service the Beaches should be getting.",
    "😞 Last week fell short. Again.",
]
WEEK_VERY_BAD_LINES = [
    "😞 A really bad week for Beaches commuters.",
    "😔 Hard to sugar-coat this one. A really poor week.",
    "😠 Not acceptable. Far too many buses let people down last week.",
    "😞 A very poor week. Commuters deserve far better.",
]
WEEK_LATE_LINES = [
    "😞 Disappointing week: too many buses running late.",
    "😔 Lots of late running last week. Not good enough.",
    "😕 If your bus was late last week, you weren't alone.",
]


# Service rating, same bands as the live dial on the site. Takes the worse of the whole period and the
# AM / PM peaks, so a terrible evening peak isn't averaged away by a quiet middle of the day.
RATINGS = [("Good", "#2e9e3e"), ("Fair", "#f2c200"), ("Poor", "#f08a00"), ("Bad", "#d62d20"), ("Extremely bad", "#8e0e0e")]
CXL_STEPS = [1, 3, 6, 10]      # % cancelled: upper edge of Good, Fair, Poor, Bad
OTR_STEPS = [95, 90, 85, 75]   # % on time: lower edge of Good, Fair, Poor, Bad (TfNSW contract target is 95%)


def _pos(v, steps, up):
    v = round(v, 1)   # rate the same one-decimal figure the post shows
    e = [0 if up else 100, *steps, 20 if up else 50]
    for i in range(5):
        lo, hi = e[i], e[i + 1]
        if (v <= hi) if up else (v >= hi):   # a value on the edge belongs to the better band
            return i + min((v - lo) / (hi - lo), 0.999)
    return 4.97


def rating(p):
    """Return (position 0..5, 'cxl' or 'late') for a period dict from period()."""
    cands = []
    if p["sched"]:
        cands.append((_pos(100 * p["cxl"] / p["sched"], CXL_STEPS, True), "cxl"))
    if p["obs"] >= 100:
        cands.append((_pos(100 * p["on"] / p["obs"], OTR_STEPS, False), "late"))
    pk = p.get("peak") or {}
    for side in ("am", "pm"):
        if pk.get(f"{side}_scheduled", 0) >= 30:
            cands.append((_pos(100 * pk[f"{side}_cancelled"] / pk[f"{side}_scheduled"], CXL_STEPS, True), "cxl"))
        if pk.get(f"{side}_observed", 0) >= 50:   # peaks record late, not on time: early buses count as on time here
            cands.append((_pos(100 - 100 * pk[f"{side}_late"] / pk[f"{side}_observed"], OTR_STEPS, False), "late"))
    if not cands:
        return 0.0, "cxl"
    s, why = max(cands)
    return max(0.0, min(4.97, s)), why


def tone(d, today, pools=None):
    """Disappointed opening line when the rating is Poor or worse, else ''."""
    very_bad, bad, late = pools or (VERY_BAD_LINES, BAD_LINES, LATE_LINES)
    s, why = rating(d)
    lvl = int(s)
    # Different line each day; stepping by 5 (coprime with every pool size) means no repeat on back-to-back days.
    pick = lambda lines: lines[(today.toordinal() * 5) % len(lines)]
    if lvl < 2:
        return ""
    if why == "late":
        return pick(late)
    return pick(very_bad if lvl >= 3 else bad)


def draw_dial(im, cx, cy, r, score, label):
    """Corner dial: five coloured wedges, needle, and the rating underneath."""
    import math
    d = ImageDraw.Draw(im)
    ro = r + 14
    d.pieslice((cx - ro, cy - ro, cx + ro, cy + ro), 180, 360, fill="#0b1a30")
    for i, (_, col) in enumerate(RATINGS):
        d.pieslice((cx - r, cy - r, cx + r, cy + r), 180 + 36 * i, 216 + 36 * i, fill=col, outline="#0b1a30", width=3)
    d.rectangle((cx - ro, cy, cx + ro, cy + 8), fill="#0b1a30")
    ang = math.radians(180 - score * 36)
    tip = (cx + (r - 8) * math.cos(ang), cy - (r - 8) * math.sin(ang))
    b1 = (cx + 9 * math.cos(ang + math.pi / 2), cy - 9 * math.sin(ang + math.pi / 2))
    b2 = (cx + 9 * math.cos(ang - math.pi / 2), cy - 9 * math.sin(ang - math.pi / 2))
    d.polygon([tip, b1, b2], fill="white", outline="#0b1a30", width=3)
    d.ellipse((cx - 13, cy - 13, cx + 13, cy + 13), fill="white", outline="#0b1a30", width=3)
    name, col = RATINGS[int(score)]
    txt_col = {"Fair": "#ffd84d", "Poor": "#ffa53d", "Bad": "#ff7b6e", "Extremely bad": "#ff4d4d"}.get(name, "#5fd36f")
    f1, f2 = font(22), font(30, True)
    d.text((cx - d.textlength(label, font=f1) / 2, cy + 16), label, font=f1, fill="#9fc3f0")
    d.text((cx - d.textlength(name.upper(), font=f2) / 2, cy + 42), name.upper(), font=f2, fill=txt_col)


def grabbed_at(now):
    """Time the figures were last collected (summary.json 'updated'), Sydney time, e.g. '5:32 pm'."""
    try:
        with open(os.path.join(DATA, "summary.json")) as fh:
            t = dt.datetime.fromisoformat(json.load(fh)["updated"]).astimezone(SYD)
        if t.date() == now.date():
            now = t
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return now.strftime("%-I:%M %p").lower()


def figures():
    now = dt.datetime.now(SYD)
    today = now.date()
    at = grabbed_at(now)
    daily = rows("history/r8_daily.csv")
    routes = rows("history/r8_routes_daily.csv")
    span = lambda k: {(today - dt.timedelta(days=i)).isoformat() for i in range(k)}
    d, w, m = period(daily, routes, span(1)), period(daily, routes, span(7)), period(daily, routes, span(30))
    if not d["sched"]:
        sys.exit("No figures for today yet; nothing to post.")
    def since(p, full):
        if p["days"] >= full:
            return ""
        return f" (since {dt.date.fromisoformat(p['first']).strftime('%-d %b')})"
    ontime = lambda p, need: f", {pct(p["on"], p["obs"])} on time" if p["obs"] >= need else ""
    when = today.strftime("%A %-d %B")
    mood = tone(d, today)
    show_month = m["days"] > w["days"]   # hide 30 days until there's more than a week of data
    text = "\n".join(
        [f"🚍 Northern Beaches buses – {when}, as at {at} 🌊", ""]
        + ([mood, ""] if mood else [])
        + [f"🚌 Today: {d['cxl']} cancelled ({pct(d['cxl'], d['sched'])}){ontime(d, 100)}",
           f"📅 Last 7 days: {w['cxl']:,} cancelled ({pct(w['cxl'], w['sched'])}){ontime(w, 300)}"]
        + ([f"🗓️ Last 30 days: {m['cxl']:,} cancelled ({pct(m['cxl'], m['sched'])}){ontime(m, 300)}"] if show_month else [])
        + ([f"🚩 Worst route this week: {w['worst'][0]} ({w['worst'][1]})"] if w["worst"] else [])
        + ["",
           "ℹ️ Keolis Region 8 only. School & charter services excluded.",
           f"👉 Full figures: {SITE}",
           f"📲 Free app: {INSTALL}",
           FOLLOW])
    blocks = [("Today so far", d, ""), ("Last 7 days", w, since(w, 7).strip(" ()"))]
    if show_month:
        blocks.append(("Last 30 days", m, since(m, 30).strip(" ()")))
    alt = (f"Northern Beaches bus figures for {when} as at {at}: today {d['cxl']} cancelled, last 7 days {w['cxl']}, "
           f"last 30 days {m['cxl']}.")
    return {"when": when, "short": today.strftime("%a %-d %b"), "at": at, "blocks": blocks, "text": text, "alt": alt,
            "worst": w["worst"], "score": rating(d)[0]}


def weekly_figures():
    """Monday morning week in review: Monday to Sunday just gone, compared with the week before."""
    today = dt.datetime.now(SYD).date()
    end = today - dt.timedelta(days=today.weekday() + 1)          # last Sunday
    week = [(end - dt.timedelta(days=i)).isoformat() for i in range(7)]
    prev = {(end - dt.timedelta(days=i)).isoformat() for i in range(7, 14)}
    daily = rows("history/r8_daily.csv")
    routes = rows("history/r8_routes_daily.csv")
    w, p = period(daily, routes, set(week)), period(daily, routes, prev)
    if not w["sched"]:
        sys.exit("No figures for last week yet; nothing to post.")
    per = {}
    for r in routes:
        if r["date"] in week:
            a = per.setdefault(r["route"], [0, 0])
            a[0] += n(r["scheduled"]); a[1] += n(r["cancelled"])
    worst = sorted(((rt, c, sc) for rt, (sc, c) in per.items() if c and sc >= 20),
                   key=lambda x: (-x[1], -x[1] / x[2]))[:3]
    days = {r["date"]: r for r in daily if r["date"] in week and n(r["scheduled"])}
    bad_day = max(days.values(), key=lambda r: n(r["cancelled"]) / n(r["scheduled"]), default=None)
    first = dt.date.fromisoformat(w["first"] or week[-1])
    when = f"{first.strftime('%-d %b')} – {end.strftime('%-d %b')}"
    partial = f" ({w['days']} days tracked)" if w["days"] < 7 else ""
    change = ""
    if p["days"] == 7 and w["days"] == 7 and p["sched"]:
        diff = 100 * w["cxl"] / w["sched"] - 100 * p["cxl"] / p["sched"]
        change = ("📈 Worse than the week before" if diff > 0.2 else "📉 Better than the week before" if diff < -0.2 else "↔️ Similar to the week before")
    on = f", {pct(w['on'], w['obs'])} on time" if w["obs"] >= 300 else ""
    mood = tone(w, end, (WEEK_VERY_BAD_LINES, WEEK_BAD_LINES, WEEK_LATE_LINES))
    bd = (f"📆 Worst day: {dt.date.fromisoformat(bad_day['date']).strftime('%A')} "
          f"({n(bad_day['cancelled'])} cancelled, {pct(n(bad_day['cancelled']), n(bad_day['scheduled']))})") if bad_day and n(bad_day["cancelled"]) else ""
    text = "\n".join(
        [f"🗓️ Northern Beaches buses – week in review 🚍 {when}{partial}", ""]
        + ([mood, ""] if mood else [])
        + [f"🚌 Last week: {w['cxl']:,} cancelled ({pct(w['cxl'], w['sched'])}){on}"]
        + ([change] if change else [])
        + ([bd] if bd else [])
        + ([f"🚩 Worst routes: " + ", ".join(f"{rt} ({c})" for rt, c, _ in worst)] if worst else [])
        + ["",
           "ℹ️ Keolis Region 8 only. School & charter services excluded.",
           f"👉 Full figures: {SITE}",
           f"📲 Free app: {INSTALL}",
           FOLLOW])
    return {"when": when + partial, "total": w, "worst": worst, "bad_day": bd, "change": change, "text": text,
            "score": rating(w)[0]}


def card_data(day=None):
    """Figures for the daily image (tools/card_styles.py). day: 'YYYY-MM-DD', default today."""
    now = dt.datetime.now(SYD)
    today = dt.date.fromisoformat(day) if day else now.date()
    daily = rows("history/r8_daily.csv")
    routes = rows("history/r8_routes_daily.csv")
    d = period(daily, routes, {today.isoformat()})
    w = period(daily, routes, {(today - dt.timedelta(days=i)).isoformat() for i in range(7)})
    pk = d["peak"]
    score = rating(d)[0] if d["sched"] else 0
    name, col = RATINGS[min(int(score), 4)]
    return {"when": today.strftime("%A %-d %B"), "at": grabbed_at(now) if not day else "5:30 pm",
            "cxl": d["cxl"], "sched": d["sched"], "cxl_pct": pct(d["cxl"], d["sched"]),
            "cxl_f": d["cxl"] / d["sched"] if d["sched"] else 0,
            "on_pct": 100 * d["on"] / d["obs"] if d["obs"] >= 100 else None, "obs": d["obs"],
            "am": (pct(pk["am_cancelled"], pk["am_scheduled"]), pk["am_cancelled"]),
            "pm": (pct(pk["pm_cancelled"], pk["pm_scheduled"]), pk["pm_cancelled"]),
            "worst": d["worst"], "wk_cxl": w["cxl"], "wk_pct": pct(w["cxl"], w["sched"]),
            "wk_f": w["cxl"] / w["sched"] if w["sched"] else 0,
            "wk_on": pct(w["on"], w["obs"]) if w["obs"] >= 300 else "", "wk_on_f": w["on"] / w["obs"] if w["obs"] >= 300 else 0,
            "score": score, "rating": name, "rcol": col}


def draw_weekly_new(f, path):
    t = f["total"]
    strip = lambda s: re.sub(r"^[^\w(]+", "", s) if s else ""
    W = {"when": f["when"], "total": t, "score": f["score"], "worst": f["worst"], "pct": pct(t["cxl"], t["sched"]),
         "on": pct(t["on"], t["obs"]) if t["obs"] >= 300 else "", "bad_day": strip(f["bad_day"]), "change": strip(f["change"])}
    card_styles.draw_weekly(W, path, dt.datetime.now(SYD).date())


def draw_weekly(f, path):
    im = Image.new("RGB", (1080, 1080), "#0f2747")
    d = ImageDraw.Draw(im)
    d.text((70, 70), "Week in review", font=font(56, True), fill="white")
    d.text((70, 140), "Keolis Northern Beaches", font=font(32), fill="#9fc3f0")
    d.text((70, 178), f["when"], font=font(26), fill="#9fc3f0")
    draw_dial(im, 912, 128, 80, f["score"], "Last week")
    t = f["total"]
    d.rounded_rectangle((60, 215, 1020, 420), radius=24, fill="#16335c")
    d.text((95, 240), "Last week", font=font(36, True), fill="#9fc3f0")
    d.text((92, 290), f"{t['cxl']:,}", font=font(96, True), fill="white")
    x = 110 + d.textlength(f"{t['cxl']:,}", font=font(96, True))
    d.text((x, 310), "cancelled", font=font(36), fill="white")
    d.text((x, 355), f"{pct(t['cxl'], t['sched'])} of {t['sched']:,} trips", font=font(30), fill="#cfe0f5")
    if t["obs"] >= 300:
        on = pct(t["on"], t["obs"])
        d.text((1000 - d.textlength(on, font=font(52, True)), 295), on, font=font(52, True), fill="white")
        d.text((1000 - d.textlength("on time", font=font(28)), 360), "on time", font=font(28), fill="#cfe0f5")
    y = 450
    for line in (f["bad_day"], f["change"]):
        if line:
            d.text((70, y), re.sub(r"^[^\w(]+", "", line), font=font(32), fill="#cfe0f5")
            y += 50
    y += 15
    if f["worst"]:
        d.text((70, y), "Most cancelled routes", font=font(32, True), fill="#9fc3f0")
        y += 55
        top = max(c for _, c, _ in f["worst"])
        for rt, c, sc in f["worst"]:
            d.rounded_rectangle((60, y, 1020, y + 95), radius=20, fill="#16335c")
            d.text((90, y + 18), rt, font=font(48, True), fill="white")
            d.rounded_rectangle((300, y + 27, 300 + max(int(420 * c / top), 8), y + 68), radius=8, fill="#e8a33d")
            lab = f"{c}  ({pct(c, sc)})"
            d.text((1000 - d.textlength(lab, font=font(32, True)), y + 28), lab, font=font(32, True), fill="white")
            y += 108
    d.text((70, 970), "Keolis Region 8 only · School & charter excluded · Data: TfNSW", font=font(26), fill="#6f8fb8")
    d.text((70, 1010), SITE.replace("https://", "").rstrip("/"), font=font(24), fill="#6f8fb8")
    im.save(path, "PNG", optimize=True)


def font(size, bold=False):
    p = f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf"
    return ImageFont.truetype(p, size) if os.path.exists(p) else ImageFont.load_default(size)


def draw_card(f, path):
    im = Image.new("RGB", (1080, 1080), "#0f2747")
    d = ImageDraw.Draw(im)
    d.text((70, 70), "Northern Beaches buses", font=font(52, True), fill="white")
    d.text((70, 140), f"Keolis cancellations · {f['short']}", font=font(34), fill="#9fc3f0")
    d.text((70, 182), f"Figures as at {f['at']}", font=font(26), fill="#9fc3f0")
    draw_dial(im, 912, 128, 80, f["score"], "Today")
    y = 215 + 115 * (3 - len(f["blocks"]))   # centre when the 30-day box is hidden
    for label, p, note in f["blocks"]:
        d.rounded_rectangle((60, y, 1020, y + 205), radius=24, fill="#16335c")
        d.text((95, y + 25), label, font=font(36, True), fill="#9fc3f0")
        if note:
            d.text((95 + d.textlength(label, font=font(36, True)) + 18, y + 33), note, font=font(26), fill="#6f8fb8")
        d.text((92, y + 75), f"{p['cxl']:,}", font=font(96, True), fill="white")
        x = 110 + d.textlength(f"{p['cxl']:,}", font=font(96, True))
        d.text((x, y + 95), "cancelled", font=font(36), fill="white")
        d.text((x, y + 140), f"{pct(p['cxl'], p['sched'])} of {p['sched']:,} trips", font=font(30), fill="#cfe0f5")
        if p["obs"] >= 100:
            on = f"{pct(p['on'], p['obs'])}"
            d.text((1000 - d.textlength(on, font=font(52, True)), y + 80), on, font=font(52, True), fill="white")
            d.text((1000 - d.textlength("on time", font=font(28)), y + 145), "on time", font=font(28), fill="#cfe0f5")
        y += 230
    if f["worst"]:
        d.text((70, 915), f"Most cancelled route this week: {f['worst'][0]} ({f['worst'][1]})", font=font(32), fill="#cfe0f5")
    d.text((70, 970), "Keolis Region 8 only · School & charter excluded · Data: TfNSW", font=font(26), fill="#6f8fb8")
    d.text((70, 1010), SITE.replace("https://", "").rstrip("/"), font=font(24), fill="#6f8fb8")
    im.save(path, "PNG", optimize=True)


def draw_story(D, path):
    """1080x1920 portrait story card: yesterday's result + rating dial. Light and simple to read on a phone."""
    W, H = 1080, 1920
    im = Image.new("RGB", (W, H), "#ffffff")
    top, bot = (126, 196, 240), (238, 247, 255)
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    navy, grey = "#0f2747", "#4a5f7a"
    c = lambda txt, y, f, col: d.text(((W - d.textlength(txt, font=f)) / 2, y), txt, font=f, fill=col)
    c("Good morning, Beaches", 170, font(64, True), navy)
    c(f"How the buses went {D['short_day']}", 260, font(40), grey)
    d.rounded_rectangle((90, 360, 990, 830), radius=40, fill="#0f2747")
    draw_dial(im, W // 2, 660, 230, D["score"], "Yesterday")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((90, 880, 990, 1380), radius=40, fill="white")
    c(f"{D['cxl']:,}", 920, font(150, True), navy)
    c(f"buses cancelled ({D['cxl_pct']})", 1100, font(44), grey)
    if D.get("on_pct") is not None:
        c(f"{D['on_pct']:.0f}% ran on time", 1180, font(44, True), navy)
    if D.get("worst"):
        c(f"Worst route: {D['worst'][0]} ({D['worst'][1]} cancelled)", 1265, font(36), grey)
    c("Live status all day:", 1490, font(44, True), navy)
    c("nbbuswatch.github.io", 1555, font(52, True), "#1565c0")
    c("Follow the page for daily updates", 1680, font(40), navy)
    c("Keolis Region 8 only · School & charter excluded · Data: TfNSW", 1830, font(26), grey)
    im.save(path, "PNG", optimize=True)


def post_story(image_path):
    """Upload the image unpublished, then publish it as a Page story (stories last 24 hours)."""
    global LAST_ERROR
    page, token = (os.environ.get("FB_PAGE_ID", "").strip() or PAGE_ID), os.environ.get("FB_PAGE_TOKEN", "").strip()
    with open(image_path, "rb") as fh:
        r = requests.post(f"{FB_API}/{page}/photos", data={"published": "false", "access_token": token},
                          files={"source": ("story.png", fh, "image/png")}, timeout=60)
    if r.status_code < 400:
        r = requests.post(f"{FB_API}/{page}/photo_stories",
                          data={"photo_id": r.json().get("id", ""), "access_token": token}, timeout=60)
    if r.status_code >= 400:
        LAST_ERROR = re.sub(r"access_token=[^&\s]+", "access_token=***", r.text[:300])
        print("Facebook story error:", LAST_ERROR)
        return False
    print("Facebook: story posted")
    return True


def story_figures():
    yday = (dt.datetime.now(SYD).date() - dt.timedelta(days=1))
    D = card_data(yday.isoformat())
    if not D["sched"]:
        sys.exit("No figures for yesterday; no story.")
    D["short_day"] = yday.strftime("on %A %-d %b")
    return D


def post_facebook(text, image_path):
    page, token = (os.environ.get("FB_PAGE_ID", "").strip() or PAGE_ID), os.environ.get("FB_PAGE_TOKEN", "").strip()
    with open(image_path, "rb") as fh:
        r = requests.post(f"{FB_API}/{page}/photos", data={"caption": text, "access_token": token},
                          files={"source": ("card.png", fh, "image/png")}, timeout=60)
    if r.status_code >= 400:
        global LAST_ERROR
        LAST_ERROR = re.sub(r"access_token=[^&\s]+", "access_token=***", r.text[:300])
        print("Facebook error:", LAST_ERROR)
        return False
    print("Facebook: posted")
    return True


def configured():
    return bool((os.environ.get("FB_PAGE_ID", "").strip() or PAGE_ID) and os.environ.get("FB_PAGE_TOKEN", "").strip())


FOLLOW = "👍 Follow the page for daily Beaches bus updates 🚌"


def explain(err):
    """Turn a Facebook error into a plain reason, so the alert says what to fix."""
    if '"code":190' in err or '"code":200' in err or "OAuthException" in err:
        return "TOKEN DEAD: replace the FB_PAGE_TOKEN secret. " + err
    return err


# Hashtags added to the end of every Facebook post. Neutral: no operator-blaming or political tags.
HASHTAGS = {
    "summary": "#NorthernBeachesBusWatch #NorthernBeaches #NorthernBeachesBuses #BLine #SydneyBuses",
    "bad":     "#NorthernBeachesBusWatch #NorthernBeaches #BusCancellations #SydneyCommute #BLine",
    "weekly":  "#NorthernBeachesBusWatch #NorthernBeaches #SydneyCommute #PublicTransport #TransportNSW",
}


def with_hashtags(text, kind, score):
    if kind != "weekly" and score >= 3:   # Bad or Extremely bad on the dial
        kind = "bad"
    return f"{text}\n\n{HASHTAGS.get(kind, HASHTAGS['summary'])}"


def publish(kind="summary", dry=False):
    global LAST_ERROR
    os.makedirs(OUT_DIR, exist_ok=True)
    img = os.path.join(OUT_DIR, f"{kind}.png")
    if kind == "story":   # morning story: image only, no caption
        draw_story(story_figures(), img)
        print("Story image:", img)
        if dry:
            return True
        try:
            return post_story(img)
        except Exception as exc:
            global LAST_ERROR
            LAST_ERROR = f"{type(exc).__name__}: {str(exc)[:200]}"
            print("Facebook story failed:", LAST_ERROR)
            return False
    if kind == "weekly":   # Monday morning week in review
        f = weekly_figures()
        draw_weekly_new(f, img)
    else:
        f = figures()
        print("Image style:", card_styles.draw_daily(card_data(), img, dt.datetime.now(SYD).date()))
    f["text"] = with_hashtags(f["text"], kind, f.get("score", 0))
    print(f["text"])
    if dry:
        return True
    try:
        return post_facebook(f["text"], img)
    except Exception as exc:
        LAST_ERROR = f"{type(exc).__name__}: {str(exc)[:200]}"
        print("Facebook failed:", LAST_ERROR)
        return False


def auto():
    """Called every few minutes. Daily summary after POST_AT; week in review on Mondays after WEEKLY_AT.
    Each posts once, retrying a failed post up to MAX_TRIES times."""
    now = dt.datetime.now(SYD)
    if not configured():
        return
    try:
        with open(STATE) as fh:
            state = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    fb = state.setdefault("facebook", {})
    today = now.date().isoformat()
    due = []
    if now.weekday() == 0 and (now.hour, now.minute) >= WEEKLY_AT and now.hour < WEEKLY_UNTIL:
        due.append("weekly")
    if (now.hour, now.minute) >= STORY_AT and now.hour < STORY_UNTIL:
        due.append("story")
    if (now.hour, now.minute) >= POST_AT and now.hour < POST_UNTIL:
        due.append("summary")
    changed = False
    for kind in due:
        tries = fb.get(f"{kind}_tries", {}).get(today, 0)
        if fb.get(kind) == today or tries >= MAX_TRIES:
            continue
        try:
            ok = publish(kind)
        except SystemExit as e:  # no figures yet
            print(e)
            ok = False
        if ok:
            fb[kind] = today
        else:
            fb[f"{kind}_tries"] = {today: tries + 1}
            fb["last_error"] = f"{now.isoformat(timespec='minutes')} {kind}: {explain(LAST_ERROR) if LAST_ERROR else 'no figures yet'}"
        changed = True
    if changed:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(STATE, "w") as fh:
            json.dump(state, fh, indent=1)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    dry = "--dry-run" in sys.argv
    if mode == "auto":
        return auto()
    if not configured() and not dry:
        sys.exit("The Facebook Page isn't set up yet (add the FB_PAGE_ID and FB_PAGE_TOKEN secrets first).")
    if not publish(mode if mode in ("summary", "weekly", "story") else "summary", dry=dry):
        sys.exit(1)


if __name__ == "__main__":
    main()
