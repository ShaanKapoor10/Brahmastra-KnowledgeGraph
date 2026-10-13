"""Draws the README diagrams, each in a light and a dark variant.

    python docs/assets/readme/build.py

Every diagram is a self-contained SVG: no scripts, no external fonts, no
images. That is what lets GitHub animate them inside an <img>. Motion is CSS
keyframes only, and `prefers-reduced-motion` turns it off. The README picks a
variant with <picture> and `prefers-color-scheme`.

The colours are the dashboard's own (frontend/app/globals.css: amber primary,
cyan, green, rose and blue cluster colours), converted from OKLCH. The light
variant keeps each hue and darkens it until it reads on paper.

Edit a diagram here and re-run; never hand-edit the generated .svg files,
or the two variants drift apart.
"""

from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent

DARK = dict(
    bg="#0A0A0C", card="#141417", card2="#1B1B1F", line="#2D2D31",
    fg="#F1F1F4", mut="#929296",
    amber="#F2B036", cyan="#14BBC2", green="#67B36A", rose="#EA6878", blue="#73A1DC",
    glow="0.22",
)
LIGHT = dict(
    bg="#FDFCF9", card="#F5F3EF", card2="#ECE9E3", line="#DAD7D2",
    fg="#1A1A1F", mut="#626369",
    amber="#B86B00", cyan="#007A90", green="#257C3A", rose="#C53443", blue="#3461AC",
    glow="0.14",
)
HUES = ("amber", "cyan", "green", "rose", "blue")

SANS = "ui-sans-serif, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'Cascadia Mono', Menlo, Consolas, monospace"


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=14, fill="fg", weight=400, anchor="start", mono=False,
         cls="", spacing=None, opacity=None, P=None):
    fam = "m" if mono else "s"
    attrs = [f'x="{x}"', f'y="{y}"', f'font-size="{size}"', f'class="{(fam + " " + cls).strip()}"',
             f'fill="{P[fill] if P and fill in P else fill}"']
    if weight != 400:
        attrs.append(f'font-weight="{weight}"')
    if anchor != "start":
        attrs.append(f'text-anchor="{anchor}"')
    if spacing is not None:
        attrs.append(f'letter-spacing="{spacing}"')
    if opacity is not None:
        attrs.append(f'opacity="{opacity}"')
    return f'<text {" ".join(attrs)}>{esc(s)}</text>'


def box(x, y, w, h, P, r=12, fill="card", stroke="line", sw=1.2, extra=""):
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" '
            f'fill="{P.get(fill, fill)}" stroke="{P.get(stroke, stroke)}" '
            f'stroke-width="{sw}" {extra}/>')


def label(x, y, s, P, fill="mut", anchor="start"):
    """Small uppercase section label."""
    return text(x, y, s.upper(), 11, fill, 600, anchor, spacing="1.6", P=P)


def line(x1, y1, x2, y2, P, color="line", sw=1.6, cls="", arrow=None, dash=None, opacity=None):
    a = f' marker-end="url(#a-{arrow})"' if arrow else ""
    d = f' stroke-dasharray="{dash}"' if dash else ""
    o = f' opacity="{opacity}"' if opacity is not None else ""
    c = f' class="{cls}"' if cls else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{P.get(color, color)}" '
            f'stroke-width="{sw}" stroke-linecap="round"{a}{d}{o}{c}/>')


def path(d, P, color="line", sw=1.6, cls="", arrow=None, opacity=None, fill="none"):
    a = f' marker-end="url(#a-{arrow})"' if arrow else ""
    o = f' opacity="{opacity}"' if opacity is not None else ""
    c = f' class="{cls}"' if cls else ""
    return (f'<path d="{d}" fill="{fill}" stroke="{P.get(color, color)}" stroke-width="{sw}" '
            f'stroke-linecap="round" stroke-linejoin="round"{a}{o}{c}/>')


def dot(cx, cy, r, P, color, cls="", stroke=None, opacity=None):
    s = f' stroke="{P[stroke] if stroke in P else stroke}" stroke-width="2"' if stroke else ""
    o = f' opacity="{opacity}"' if opacity is not None else ""
    c = f' class="{cls}"' if cls else ""
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{P.get(color, color)}"{s}{o}{c}/>'


def triple(x, y, subj, rel, obj, P, size=12, hue="amber", anchor="start", fill="fg"):
    """subject relation object, with the relation in colour (SVG collapses double spaces)."""
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" class="m" fill="{P[fill]}"{a}>{esc(subj)} '
            f'<tspan fill="{P[hue]}" font-weight="600">{esc(rel)}</tspan> {esc(obj)}</text>')


def triple_pill(x, y, subj, rel, obj, P, color, size=12, h=30, pad=14):
    w = int(len(f"{subj} {rel} {obj}") * size * 0.62) + pad * 2
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h/2}" fill="{P["bg"]}" '
            f'stroke="{P[color]}" stroke-width="1.4"/>'
            + triple(x + w / 2, y + h / 2 + size * 0.36, subj, rel, obj, P, size, color, "middle")), w


def pill(x, y, s, P, color, size=11, pad=9, h=20, filled=False, mono=True):
    w = int(len(s) * size * (0.62 if mono else 0.58)) + pad * 2
    fill = P[color] if filled else P["bg"]
    tfill = P["bg"] if filled else P[color]
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h/2}" fill="{fill}" '
            f'stroke="{P[color]}" stroke-width="1.2"/>'
            + text(x + w / 2, y + h / 2 + size * 0.36, s, size, tfill, 600, "middle", mono=mono, P=P)), w


# --------------------------------------------------------------------------
# motion
# --------------------------------------------------------------------------

BASE_CSS = """
.s{font-family:%(sans)s}
.m{font-family:%(mono)s}
.flow{stroke-dasharray:6 9;animation:mv 1.5s linear infinite}
.flow-slow{stroke-dasharray:4 10;animation:mv 3s linear infinite}
.flow-back{stroke-dasharray:6 9;animation:mv 1.5s linear infinite reverse}
@keyframes mv{to{stroke-dashoffset:-30}}
.pulse{animation:pl 2.6s ease-in-out infinite;transform-box:fill-box;transform-origin:center}
@keyframes pl{0%%,100%%{transform:scale(1)}50%%{transform:scale(1.22)}}
.breathe{animation:br 3.2s ease-in-out infinite}
@keyframes br{0%%,100%%{opacity:.35}50%%{opacity:1}}
.blink{animation:bk 1.6s steps(2,start) infinite}
@keyframes bk{to{visibility:hidden}}
@media (prefers-reduced-motion: reduce){*{animation:none!important}}
""" % dict(sans=SANS, mono=MONO)


class Motion:
    """Collects per-diagram keyframes: timed reveals and moving packets."""

    def __init__(self, period: float):
        self.period = period
        self.css: list[str] = []
        self.n = 0
        # Every loop starts at 90%: the finished frame. A viewer that shows only
        # the first frame (a preview, a cache, a print) then gets the whole
        # diagram rather than an empty one, and the story replays from there.
        self.lead = f"-{period * 0.9:.2f}s"

    def reveal(self, start: float, end: float = 0.93, rise: int = 6) -> str:
        """Class that fades in at `start` (fraction of the loop) and out at `end`."""
        self.n += 1
        k = f"r{self.n}"
        a = round(start * 100, 2)
        b = round(min(start + 0.04, end) * 100, 2)
        e = round(end * 100, 2)
        f = round(min(end + 0.03, 1) * 100, 2)
        self.css.append(
            f"@keyframes {k}{{0%,{a}%{{opacity:0;transform:translateY({rise}px)}}"
            f"{b}%,{e}%{{opacity:1;transform:translateY(0)}}{f}%,100%{{opacity:0}}}}"
            f".{k}{{animation:{k} {self.period}s ease-out {self.lead} infinite;transform-box:fill-box}}")
        return k

    def window(self, start: float, end: float) -> str:
        """Visible only between `start` and `end`, used for highlights."""
        self.n += 1
        k = f"w{self.n}"
        a, b = round(start * 100, 2), round(min(start + 0.02, end) * 100, 2)
        c, d = round(max(end - 0.02, start) * 100, 2), round(end * 100, 2)
        self.css.append(
            f"@keyframes {k}{{0%,{a}%{{opacity:0}}{b}%,{c}%{{opacity:1}}{d}%,100%{{opacity:0}}}}"
            f".{k}{{animation:{k} {self.period}s linear {self.lead} infinite}}")
        return k

    def strike(self, start: float, length: float, end: float = 0.93) -> str:
        """A line that draws itself at `start` and stays until `end`."""
        self.n += 1
        k = f"k{self.n}"
        a, b, e = round(start * 100, 2), round(min(start + 0.05, end) * 100, 2), round(end * 100, 2)
        self.css.append(
            f"@keyframes {k}{{0%,{a}%{{stroke-dashoffset:{length}}}{b}%,{e}%{{stroke-dashoffset:0}}"
            f"{min(e + 3, 100)}%,100%{{stroke-dashoffset:{length}}}}}"
            f".{k}{{stroke-dasharray:{length};animation:{k} {self.period}s ease-in-out {self.lead} infinite}}")
        return k

    def packet(self, pts: list[tuple[float, float]], dur: float, delay: float = 0) -> str:
        """Class that moves a dot along straight segments through `pts`."""
        self.n += 1
        k = f"p{self.n}"
        steps = len(pts) - 1
        frames = []
        for i, (x, y) in enumerate(pts):
            pct = round(i / steps * 88 + 4, 2)
            frames.append(f"{pct}%{{transform:translate({x}px,{y}px)}}")
        x0, y0 = pts[0]
        xn, yn = pts[-1]
        self.css.append(
            f"@keyframes {k}{{0%{{transform:translate({x0}px,{y0}px);opacity:0}}"
            f"4%{{opacity:1}}{''.join(frames)}92%{{opacity:1}}100%{{transform:translate({xn}px,{yn}px);opacity:0}}}}"
            f".{k}{{animation:{k} {dur}s linear {delay}s infinite}}")
        return k

    def slide(self, dx: float, dy: float, start: float, hold: float, back: float) -> str:
        """Moves by (dx,dy) between `start` and `hold`, returns by `back`."""
        self.n += 1
        k = f"d{self.n}"
        a, b, c = (round(v * 100, 2) for v in (start, hold, back))
        self.css.append(
            f"@keyframes {k}{{0%,{a}%{{transform:translate(0,0)}}{b}%,{c}%"
            f"{{transform:translate({dx}px,{dy}px)}}{min(c + 6, 100)}%,100%{{transform:translate(0,0)}}}}"
            f".{k}{{animation:{k} {self.period}s ease-in-out {self.lead} infinite}}")
        return k


def markers(P) -> str:
    out = []
    for name in ("fg", "mut", "line") + HUES:
        out.append(
            f'<marker id="a-{name}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" '
            f'markerHeight="7" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" '
            f'fill="{P[name]}"/></marker>')
    return "".join(out)


def write(name: str, w: int, h: int, title: str, desc: str, draw) -> None:
    for theme, P in (("light", LIGHT), ("dark", DARK)):
        mo, body = draw(P)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" '
            f'height="{h}" role="img" aria-label="{esc(desc)}">'
            f'<title>{esc(title)}</title><desc>{esc(desc)}</desc>'
            f'<defs>{markers(P)}<style>{BASE_CSS}{"".join(mo.css) if mo else ""}</style></defs>'
            f'<rect width="{w}" height="{h}" rx="18" fill="{P["bg"]}"/>'
            f'{body}</svg>\n')
        (OUT / f"{name}-{theme}.svg").write_text(svg, encoding="utf-8")


# --------------------------------------------------------------------------
# 1. hero — sources feed one brain, every AI reads and writes it over MCP
# --------------------------------------------------------------------------

def hero(P):
    mo = Motion(8)
    o = []
    o.append(label(48, 54, "What feeds it", P))
    o.append(label(600, 54, "One shared brain", P, anchor="middle"))
    o.append(label(1152, 54, "Every AI, every device", P, anchor="end"))

    sources = [
        ("Notes", "Notion, synced both ways", "amber"),
        ("Meetings and lectures", "who said what, quoted", "cyan"),
        ("AI coding sessions", "kept before context is lost", "green"),
        ("Your code", "every symbol, file and line", "blue"),
        ("Any agent", "brahmastra_add_note", "rose"),
    ]
    cx, cy = 600, 300
    for i, (t, sub, hue) in enumerate(sources):
        y = 86 + i * 82
        o.append(box(48, y, 262, 62, P))
        o.append(dot(72, y + 31, 6, P, hue))
        o.append(text(90, y + 27, t, 15, "fg", 600, P=P))
        o.append(text(90, y + 46, sub, 12, "mut", mono=(hue == "rose"), P=P))
        sy = y + 31
        d = f"M310 {sy} C 380 {sy}, 400 {cy + (sy - cy) * 0.35}, 452 {cy + (sy - cy) * 0.25}"
        o.append(path(d, P, hue, 1.8, "flow", opacity=0.9))

    # the brain: a small graph that keeps re-forming
    o.append(f'<circle cx="{cx}" cy="{cy}" r="150" fill="{P["amber"]}" fill-opacity="{P["glow"]}" class="breathe"/>')
    o.append(f'<circle cx="{cx}" cy="{cy}" r="150" fill="none" stroke="{P["amber"]}" stroke-width="1.4" opacity="0.55"/>')
    # key: (x, y, hue, radius, name, label dx, label dy, anchor)
    nodes = {
        "a": (600, 214, "amber", 11, "Sarah Khan", 16, -8, "start"), "b": (528, 262, "amber", 7, ""),
        "c": (674, 252, "cyan", 9, "auth migration", 0, -16, "middle"), "d": (556, 330, "green", 8, ""),
        "e": (650, 340, "cyan", 7, ""), "f": (600, 290, "amber", 14, ""),
        "g": (700, 300, "blue", 8, "June 10", 0, 24, "middle"), "h": (520, 340, "green", 6, ""),
        "i": (610, 392, "rose", 9, "export API", 16, 4, "start"), "j": (706, 410, "blue", 6, ""),
        "k": (540, 216, "green", 6, ""), "l": (740, 360, "rose", 6, ""),
        "m": (482, 296, "blue", 7, "Priya", 0, 24, "middle"),
    }
    edges = "af ab ac bf cf df ef fg fh fi gj ij cg bk ak dh md mb el gl ei".split()
    for e in edges:
        x1, y1 = nodes[e[0]][:2]
        x2, y2 = nodes[e[1]][:2]
        o.append(line(x1, y1, x2, y2, P, "mut", 1.1, opacity=0.55))
    for i, (k, (x, y, hue, r, name, *at)) in enumerate(nodes.items()):
        o.append(f'<g class="pulse" style="animation-delay:{(i * 0.37) % 2.6:.2f}s">{dot(x, y, r, P, hue)}</g>')
        if name:
            dx, dy, anchor = at
            o.append(text(x + dx, y + dy, name, 11, "fg", 500, anchor, mono=True, P=P))
    o.append(text(cx, cy + 186, "BRAHMASTRA", 13, "amber", 700, "middle", spacing="4", P=P))
    o.append(text(cx, cy + 206, "facts, statements and sources, in one knowledge graph", 12, "mut", anchor="middle", P=P))

    clients = [
        ("Claude Code", "Priya, on her laptop"),
        ("Cursor", "Marco, at his desk"),
        ("Any MCP client", "Aisha, another office"),
        ("Your own agent", "running on a server"),
    ]
    for i, (t, sub) in enumerate(clients):
        y = 98 + i * 98
        o.append(box(890, y, 262, 66, P))
        o.append(f'<rect x="908" y="{y + 18}" width="30" height="22" rx="4" fill="none" stroke="{P["cyan"]}" stroke-width="1.6"/>')
        o.append(line(904, y + 44, 942, y + 44, P, "cyan", 1.6))
        o.append(text(954, y + 30, t, 15, "fg", 600, P=P))
        o.append(text(954, y + 49, sub, 12, "mut", P=P))
        ty = y + 33
        o.append(path(f"M752 {cy - 6} C 810 {cy - 6}, 820 {ty - 5}, 888 {ty - 5}", P, "cyan", 1.6, "flow", opacity=0.85))
        o.append(path(f"M888 {ty + 5} C 820 {ty + 5}, 810 {cy + 6}, 752 {cy + 6}", P, "amber", 1.6, "flow", opacity=0.85))
    o.append(box(762, 462, 112, 24, P, r=12, fill="bg", stroke="cyan"))
    o.append(text(818, 478, "MCP · 12 tools", 11, "cyan", 600, "middle", mono=True, P=P))
    o.append(line(770, 504, 794, 504, P, "cyan", 2))
    o.append(text(800, 508, "recall", 11, "cyan", 600, mono=True, P=P))
    o.append(line(770, 522, 794, 522, P, "amber", 2))
    o.append(text(800, 526, "remember", 11, "amber", 600, mono=True, P=P))

    claims = ["Remembers for months, not one chat", "Shared by everyone on the project",
              "Every answer cites where it came from"]
    for i, c in enumerate(claims):
        x = 200 + i * 400
        o.append(dot(x - 112, 548, 3, P, "amber"))
        o.append(text(x - 100, 552, c, 13, "fg", 500, P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 2. a note's journey through the pipeline
# --------------------------------------------------------------------------

def pipeline(P):
    mo = Motion(12)
    o = []
    o.append(box(40, 36, 340, 132, P))
    o.append(text(60, 62, "note · standup, 20 May", 12, "mut", mono=True, P=P))
    for i, s in enumerate(["Sarah K. owns the auth migration.", "It ships on June 10.", "She reports to Mei Lin."]):
        o.append(text(60, 92 + i * 24, s, 15, "fg", P=P))
    o.append(text(420, 70, "Seven stages. Each one is incremental:", 15, "fg", 600, P=P))
    o.append(text(420, 96, "a model reply is cached by note, model and prompt,", 14, "mut", P=P))
    o.append(text(420, 118, "so an unchanged note costs nothing to rebuild, and an", 14, "mut", P=P))
    o.append(text(420, 140, "edited one re-reads only itself.", 14, "mut", P=P))
    o.append(path("M210 168 L 210 196 L 118 196 L 118 206", P, "amber", 1.6, "flow", arrow="amber"))

    stages = ["Sync", "Extract", "Resolve", "Build graph", "Summarise", "Cache", "Write back"]
    subs = ["Notion pull", "facts + quotes", "one name each", "PageRank, topics",
            "label each topic", "serve fast", "insights to Notion"]
    pw, gap, x0, y0 = 148, 12, 46, 210
    for i, (s, sub) in enumerate(zip(stages, subs)):
        x = x0 + i * (pw + gap)
        o.append(box(x, y0, pw, 58, P, r=10))
        hl = mo.window(i / 7 * 0.9, (i + 1) / 7 * 0.9 + 0.02)
        o.append(f'<rect x="{x}" y="{y0}" width="{pw}" height="58" rx="10" fill="{P["amber"]}" '
                 f'fill-opacity="{P["glow"]}" stroke="{P["amber"]}" stroke-width="2" class="{hl}"/>')
        o.append(text(x + pw / 2, y0 + 25, s, 14, "fg", 600, "middle", P=P))
        o.append(text(x + pw / 2, y0 + 44, sub, 11, "mut", anchor="middle", mono=True, P=P))
        if i < 6:
            o.append(line(x + pw + 1, y0 + 29, x + pw + gap - 1, y0 + 29, P, "mut", 1.4, arrow="mut"))

    def stem(i, x_to):
        xm = x0 + i * (pw + gap) + pw / 2
        return path(f"M{xm} {y0 + 58} L {xm} 288 L {x_to} 288 L {x_to} 300", P, "line", 1.3)

    # Extract
    t1 = mo.reveal(1 / 7 * 0.9 + 0.01)
    o.append(stem(1, 186))
    o.append(f'<g class="{t1}">' + box(40, 300, 300, 150, P)
             + label(58, 324, "Extract", P, "amber")
             + "".join(triple(58, 352 + j * 30, *t, P) for j, t in enumerate([
                 ("Sarah K.", "owns", "auth migration"), ("auth migration", "scheduled_for", "June 10"),
                 ("Sarah K.", "reports_to", "Mei Lin")]))
             + text(58, 438, "each fact keeps the sentence it came from", 11, "mut", P=P) + "</g>")
    # Resolve
    t2 = mo.reveal(2 / 7 * 0.9 + 0.01)
    o.append(stem(2, 494))
    o.append(f'<g class="{t2}">' + box(354, 300, 280, 150, P)
             + label(372, 324, "Resolve", P, "amber")
             + "".join(pill(372 + dx, 340, s, P, "cyan")[0] for dx, s in ((0, "Sarah"), (70, "Sarah K."), (164, "Sarah Khan")))
             + line(494, 368, 494, 392, P, "green", 1.6, arrow="green")
             + pill(436, 398, "Sarah Khan", P, "green", 12, h=24, filled=True)[0]
             + text(372, 438, "spelling, initials, meaning", 11, "mut", P=P) + "</g>")
    # Build graph
    t3 = mo.reveal(3 / 7 * 0.9 + 0.01)
    o.append(stem(3, 778))
    # (x, y, r, hue, name, label x, label y, anchor)
    g = [(694, 380, 16, "amber", "Sarah Khan", 694, 352, "middle"),
         (862, 360, 9, "cyan", "auth migration", 848, 342, "end"),
         (870, 420, 7, "blue", "June 10", 856, 424, "end"),
         (694, 432, 8, "green", "Mei Lin", 708, 436, "start")]
    gb = [line(694, 380, 862, 360, P, "mut", 1.2), line(862, 360, 870, 420, P, "mut", 1.2),
          line(694, 380, 694, 432, P, "mut", 1.2)]
    gb += [dot(x, y, r, P, c, "pulse") + text(lx, ly, n, 11, "fg", anchor=an, mono=True, P=P)
           for x, y, r, c, n, lx, ly, an in g]
    o.append(f'<g class="{t3}">' + box(648, 300, 260, 150, P) + label(666, 324, "Build graph", P, "amber")
             + "".join(gb) + "</g>")
    # Summarise
    t4 = mo.reveal(4 / 7 * 0.9 + 0.01)
    o.append(stem(4, 1038))
    o.append(f'<g class="{t4}">' + box(922, 300, 238, 150, P) + label(940, 324, "Summarise", P, "amber")
             + pill(940, 338, "Auth and identity", P, "amber", 11)[0]
             + text(940, 384, "Sarah Khan's auth", 13, "fg", P=P)
             + text(940, 404, "migration ships June 10;", 13, "fg", P=P)
             + text(940, 424, "she reports to Mei Lin.", 13, "fg", P=P)
             + text(940, 442, "kept until the topic changes", 11, "mut", P=P) + "</g>")
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 3. entity resolution — merges what is the same, refuses what only looks it
# --------------------------------------------------------------------------

def resolution(P):
    mo = Motion(7)
    o = []
    o.append(label(48, 50, "Same person, three names", P))
    o.append(label(640, 50, "Looks alike, is not", P))
    o.append(line(610, 40, 610, 330, P, "line", 1.2))
    mentions = [("Sarah", "standup note", 92), ("Sarah K.", "Notion page", 170), ("Sarah Khan", "Q3 meeting", 248)]
    for s, where, y in mentions:
        dy = 176 - (y + 20)
        cls = mo.slide(70, dy * 0.45, 0.15, 0.4, 0.8)
        o.append(f'<g class="{cls}">' + box(48, y, 210, 46, P, r=23)
                 + dot(72, y + 23, 6, P, "cyan")
                 + text(88, y + 21, s, 15, "fg", 600, P=P)
                 + text(88, y + 37, where, 11, "mut", mono=True, P=P) + "</g>")
        o.append(path(f"M260 {y + 23} C 340 {y + 23}, 360 176, 428 176", P, "cyan", 1.5, "flow", arrow="cyan", opacity=0.8))
    o.append(f'<circle cx="480" cy="176" r="46" fill="{P["green"]}" fill-opacity="{P["glow"]}" class="breathe"/>')
    o.append(dot(480, 176, 30, P, "green", "pulse"))
    o.append(text(480, 181, "1", 18, "bg", 700, "middle", P=P))
    o.append(text(480, 244, "Sarah Khan", 15, "fg", 700, "middle", P=P))
    o.append(text(480, 262, "one entity, three mentions", 12, "mut", anchor="middle", P=P))
    o.append(text(48, 312, "Signals: spelling (Jaro-Winkler), initials, acronyms,", 12, "mut", P=P))
    o.append(text(48, 330, "and meaning (sentence embeddings).", 12, "mut", P=P))

    refused = [("Brahmastra", "Brahmastra v3", "a release is not the product"),
               ("backend/.env", "loading backend/.env", "an activity is not its object"),
               ("Sarah · work", "Sarah · personal", "workspaces never merge")]
    for i, (a, b, why) in enumerate(refused):
        y = 82 + i * 82
        la = mo.slide(-10, 0, 0.3 + i * 0.08, 0.36 + i * 0.08, 0.7)
        rb = mo.slide(10, 0, 0.3 + i * 0.08, 0.36 + i * 0.08, 0.7)
        pa, wa = pill(640, y, a, P, "fg", 12, 12, 30)
        pb, wb = pill(640 + wa + 52, y, b, P, "fg", 12, 12, 30)
        o.append(f'<g class="{la}">{pa}</g><g class="{rb}">{pb}</g>')
        xm = 640 + wa + 26
        o.append(f'<g class="blink" style="animation-delay:{i * 0.4}s">'
                 + line(xm - 7, y + 8, xm + 7, y + 22, P, "rose", 2.4)
                 + line(xm + 7, y + 8, xm - 7, y + 22, P, "rose", 2.4) + "</g>")
        o.append(text(640, y + 52, why, 12, "rose", 500, P=P))
    o.append(text(640, 330, "Rules on spelling decide first; a model judge only confirms.", 12, "mut", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 4. contradictions — one value at a time, the newest note wins
# --------------------------------------------------------------------------

def contradiction(P):
    mo = Motion(9)
    o = []
    o.append(line(60, 250, 760, 250, P, "line", 2))
    for x, d in ((200, "2 May"), (560, "20 May")):
        o.append(line(x, 242, x, 258, P, "mut", 2))
        o.append(text(x, 280, d, 12, "mut", 600, "middle", mono=True, P=P))
    a1, a2 = mo.reveal(0.02), mo.reveal(0.14)
    b1, b2 = mo.reveal(0.30), mo.reveal(0.42)
    o.append(f'<g class="{a1}">' + box(60, 40, 300, 86, P) + text(80, 66, "planning doc · 2 May", 12, "mut", mono=True, P=P)
             + text(80, 98, "The launch is set for May 28.", 15, "fg", P=P) + line(200, 126, 200, 160, P, "mut", 1.4) + "</g>")
    o.append(f'<g class="{b1}">' + box(420, 40, 300, 86, P) + text(440, 66, "standup · 20 May", 12, "mut", mono=True, P=P)
             + text(440, 98, "We moved the launch to June 10.", 15, "fg", P=P) + line(560, 126, 560, 160, P, "mut", 1.4) + "</g>")
    fa, wa = triple_pill(86, 166, "launch", "scheduled_for", "May 28", P, "rose")
    fb, wb = triple_pill(442, 166, "launch", "scheduled_for", "June 10", P, "green")
    st = mo.strike(0.62, wa - 20)
    o.append(f'<g class="{a2}">{fa}<line x1="96" y1="181" x2="{86 + wa - 10}" y2="181" stroke="{P["rose"]}" stroke-width="2" class="{st}"/></g>')
    o.append(f'<g class="{b2}">{fb}</g>')
    c = mo.window(0.52, 0.66)
    o.append(f'<g class="{c}">' + path(f"M{86 + wa + 6} 181 L 436 181", P, "rose", 2, "flow")
             + text((86 + wa + 442) / 2, 160, "conflict", 12, "rose", 700, "middle", P=P) + "</g>")

    r = mo.reveal(0.70)
    o.append(f'<g class="{r}">' + box(810, 40, 340, 230, P, stroke="green", sw=1.6)
             + label(830, 68, "Resolved", P, "green")
             + text(830, 104, "launch is scheduled for", 15, "fg", P=P)
             + text(830, 134, "June 10", 26, "green", 700, P=P)
             + text(830, 170, "May 28 stays in the evidence,", 13, "mut", P=P)
             + text(830, 190, "with its quote and its note.", 13, "mut", P=P)
             + text(830, 228, "Undated notes, or both said at once:", 13, "mut", P=P)
             + text(830, 248, "it answers “unresolved”, not a guess.", 13, "mut", P=P) + "</g>")
    o.append(text(60, 320, "Five relations hold one value at a time: reports_to, has_status, scheduled_for, located_in,", 13, "mut", P=P))
    o.append(text(60, 340, "employed_by. A second value is a contradiction, and the newer note wins.", 13, "mut", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 5. meetings — quoted statements become graph nodes
# --------------------------------------------------------------------------

def meetings(P):
    mo = Motion(12)
    o = []
    o.append(box(40, 40, 520, 450, P))
    o.append(text(62, 70, "Q3 release sync · transcript", 12, "mut", mono=True, P=P))
    lines = [
        ("00:04:12", "Priya", "amber", ["Let's ship the export API on Friday, not", "Monday. Monday hits the billing freeze."]),
        ("00:05:40", "Raj", "cyan", ["I'll take the reconciliation script."]),
        ("00:07:03", "Mei", "rose", ["Staging is blocked until security", "signs off."]),
        ("00:31:15", "Raj", "cyan", ["Reconciliation is done, I merged", "it this morning."]),
    ]
    ys = [100, 196, 266, 362]
    marks = []
    for i, ((ts, who, hue, words), y) in enumerate(zip(lines, ys)):
        hl = mo.window(i * 0.2 + 0.02, 0.95)
        o.append(f'<rect x="52" y="{y - 6}" width="496" height="{30 + 22 * len(words)}" rx="8" '
                 f'fill="{P[hue]}" fill-opacity="{P["glow"]}" class="{hl}"/>')
        o.append(text(64, y + 14, ts, 12, "mut", mono=True, P=P))
        o.append(text(146, y + 14, who, 14, hue, 700, P=P))
        quoted = list(words)
        quoted[0] = "“" + quoted[0]
        quoted[-1] = quoted[-1] + "”"
        for j, w in enumerate(quoted):
            o.append(text(146, y + 38 + j * 22, w, 14, "fg", P=P))
        marks.append(y + 14)

    items = [
        (640, 60, "Decision", "amber", "Ship the export API Friday", "decided_by", "Priya",
         "a statement, with its quote"),
        (640, 196, "Action item", "cyan", "Reconciliation script", "assigned_to", "Raj",
         "its status comes from what was said"),
        (640, 330, "Risk", "rose", "Staging blocked on sign-off", "raised_by", "Mei",
         "a statement, with its quote"),
    ]
    for i, (x, y, kind, hue, title, rel, who, sub) in enumerate(items):
        rv = mo.reveal(i * 0.2 + 0.08)
        g = (box(x, y, 330, 92, P, stroke=hue, sw=1.6)
             + label(x + 18, y + 26, kind, P, hue)
             + text(x + 18, y + 54, title, 15, "fg", 600, P=P)
             + text(x + 18, y + 76, sub, 11, "mut", P=P)
             + line(x + 330, y + 46, x + 410, y + 46, P, hue, 1.6, arrow=hue)
             + text(x + 370, y + 38, rel, 10, "mut", anchor="middle", mono=True, P=P)
             + dot(x + 440, y + 46, 26, P, hue)
             + text(x + 440, y + 51, who, 12, "bg", 700, "middle", P=P))
        o.append(f'<g class="{rv}">{g}</g>')
        o.append(f'<g class="{rv}">' + path(f"M548 {marks[i]} C 590 {marks[i]}, 600 {y + 46}, 638 {y + 46}", P, hue, 1.4, "flow", opacity=0.9) + "</g>")
    st = mo.reveal(0.68)
    sp, _ = pill(866, 208, "done", P, "green", 12, 14, 24, filled=True)
    o.append(f'<g class="{st}">{sp}'
             + path(f"M548 {marks[3]} C 596 {marks[3]}, 600 272, 638 272", P, "green", 1.4, "flow", opacity=0.9)
             + "</g>")
    mt = mo.reveal(0.80)
    o.append(f'<g class="{mt}">' + box(640, 446, 520, 44, P, r=22, stroke="blue", sw=1.6)
             + f'<text x="900" y="473" font-size="13" text-anchor="middle" class="s" fill="{P["mut"]}">'
               f'all three <tspan class="m" fill="{P["blue"]}" font-weight="600">discussed_in</tspan>'
               f' <tspan fill="{P["fg"]}" font-weight="600">Q3 release sync</tspan>, a meeting node</text>'
             + "</g>")
    o.append(text(40, 530, "Every point must quote the transcript or it is dropped. Each part is read twice and merged.", 13, "mut", P=P))
    o.append(text(40, 550, "Done and blocked need the words that say so; otherwise a task stays open.", 13, "mut", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 6. /ask — graph first, transcript only as a marked fallback
# --------------------------------------------------------------------------

def ask(P):
    mo = Motion(8)
    o = []
    o.append(box(40, 124, 300, 96, P, r=16))
    o.append(label(60, 152, "You ask", P))
    o.append(text(60, 180, "Why did the export API", 16, "fg", 600, P=P))
    o.append(text(60, 202, "move to Friday?", 16, "fg", 600, P=P))

    o.append(box(410, 50, 300, 130, P, stroke="green", sw=1.6))
    o.append(label(430, 78, "1 · The graph", P, "green"))
    o.append(text(430, 108, "entities matched by name", 14, "fg", P=P))
    o.append(text(430, 132, "statements matched by meaning", 14, "fg", P=P))
    o.append(text(430, 160, "facts walked 1 to 3 hops", 12, "mut", mono=True, P=P))

    o.append(box(410, 250, 300, 112, P, stroke="amber", sw=1.2, extra='opacity="0.85"'))
    o.append(label(430, 278, "2 · Only if the graph has nothing", P, "amber"))
    o.append(text(430, 306, "the raw transcript passages,", 14, "fg", P=P))
    o.append(text(430, 330, "with speaker and time", 14, "fg", P=P))

    o.append(path("M340 160 C 370 160, 380 115, 408 115", P, "green", 1.8, "flow", arrow="green"))
    o.append(path("M560 180 L 560 248", P, "amber", 1.6, "flow-slow", arrow="amber", opacity=0.8))
    o.append(text(572, 220, "NOT_IN_GRAPH", 11, "amber", 600, mono=True, P=P))
    o.append(path("M710 115 L 778 115", P, "green", 1.8, "flow", arrow="green"))
    o.append(path("M710 306 L 778 306", P, "amber", 1.6, "flow-slow", arrow="amber", opacity=0.8))

    o.append(box(780, 50, 380, 150, P, stroke="green", sw=1.6))
    gp, _ = pill(800, 66, "from the graph", P, "green", 11, 10, 22, filled=True)
    o.append(gp)
    o.append(text(800, 118, "Priya moved it to Friday: Monday", 15, "fg", P=P))
    o.append(text(800, 140, "hits the billing freeze.", 15, "fg", P=P))
    o.append(text(800, 176, "said_by Priya · Q3 release sync · 00:04:12", 11, "mut", mono=True, P=P))
    o.append(f'<rect x="780" y="50" width="380" height="150" rx="12" fill="{P["green"]}" class="breathe" fill-opacity="{P["glow"]}"/>')

    o.append(box(780, 250, 380, 112, P, stroke="amber", sw=1.2, extra='opacity="0.85"'))
    ap, _ = pill(800, 266, "from the transcript, not in the graph yet", P, "amber", 11, 10, 22)
    o.append(ap)
    o.append(text(800, 316, "Still answered, and it also tells you", 14, "fg", P=P))
    o.append(text(800, 338, "extraction missed something.", 14, "fg", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 7. storage — source is precious, the graph is a rebuildable cache
# --------------------------------------------------------------------------

def storage(P):
    mo = Motion(6)
    o = []

    def cylinder(x, y, w, h, hue):
        ry = 12
        return (f'<path d="M{x} {y + ry} L {x} {y + h - ry} A {w / 2} {ry} 0 0 0 {x + w} {y + h - ry} '
                f'L {x + w} {y + ry}" fill="{P["card2"]}" stroke="{P[hue]}" stroke-width="1.6"/>'
                f'<ellipse cx="{x + w / 2}" cy="{y + ry}" rx="{w / 2}" ry="{ry}" fill="{P["card"]}" '
                f'stroke="{P[hue]}" stroke-width="1.6"/>')

    o.append(box(40, 40, 430, 250, P, stroke="amber", sw=1.6))
    o.append(label(62, 68, "Source · cannot be recomputed", P, "amber"))
    o.append(cylinder(62, 92, 96, 110, "amber"))
    o.append(text(110, 228, "Postgres", 13, "fg", 600, "middle", P=P))
    o.append(text(110, 246, "+ pgvector", 11, "mut", anchor="middle", mono=True, P=P))
    for i, s in enumerate(["notes", "workspaces", "your review decisions"]):
        o.append(dot(190, 110 + i * 32, 4, P, "amber"))
        o.append(text(204, 115 + i * 32, s, 15, "fg", P=P))
    o.append(text(190, 222, "hybrid search: words + meaning", 12, "mut", P=P))
    o.append(text(190, 242, "back this up", 12, "amber", 600, P=P))

    o.append(box(730, 40, 430, 250, P, stroke="cyan", sw=1.2))
    o.append(label(752, 68, "Derived · a cache of the notes", P, "cyan"))
    o.append(cylinder(752, 92, 96, 110, "cyan"))
    o.append(text(800, 228, "Neo4j", 13, "fg", 600, "middle", P=P))
    o.append(text(800, 246, "or SQLite", 11, "mut", anchor="middle", mono=True, P=P))
    for i, s in enumerate(["facts and entities", "topics and summaries", "cached model replies"]):
        o.append(dot(880, 110 + i * 32, 4, P, "cyan"))
        o.append(text(894, 115 + i * 32, s, 15, "fg", P=P))
    o.append(text(880, 222, "rebuilt from the notes", 12, "mut", P=P))
    o.append(text(880, 242, "at any time", 12, "cyan", 600, P=P))

    o.append(path("M472 150 L 726 150", P, "amber", 2, "flow", arrow="amber"))
    o.append(text(600, 136, "the pipeline rebuilds", 12, "fg", 600, "middle", P=P))
    o.append(text(600, 172, "run_pipeline(full=True)", 11, "mut", anchor="middle", mono=True, P=P))
    k = mo.packet([(472, 150), (726, 150)], 2.4)
    o.append(f'<circle r="5" fill="{P["amber"]}" class="{k}"/>')

    o.append(box(40, 318, 1120, 64, P, r=12, fill="card2"))
    o.append(label(62, 344, "Ownership", P))
    o.append(text(62, 368, "Every derived record has an owner. Edit or delete a source, and everything built from it follows, automatically.", 13, "fg", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 8. session checkpoints — capture never blocks, distil fails closed
# --------------------------------------------------------------------------

def checkpoint(P):
    mo = Motion(9)
    o = []
    steps = [
        (40, "An AI session", "your Claude Code chat", "fg"),
        (250, "Capture", "file copy only, cannot block", "blue"),
        (460, "Queue on disk", "survives a crash", "blue"),
        (670, "Distil", "a model writes the note", "amber"),
        (880, "Check", "fails closed", "rose"),
    ]
    for i, (x, t, sub, hue) in enumerate(steps):
        o.append(box(x, 60, 180, 78, P, stroke=hue if hue != "fg" else "line", sw=1.4))
        o.append(text(x + 90, 92, t, 15, "fg", 600, "middle", P=P))
        o.append(text(x + 90, 114, sub, 11, "mut", anchor="middle", P=P))
        if i < 4:
            o.append(line(x + 182, 99, x + 208, 99, P, "mut", 1.6, arrow="mut"))
    k = mo.packet([(130, 99), (340, 99), (550, 99), (760, 99), (970, 99)], 4.5)
    o.append(f'<circle r="6" fill="{P["amber"]}" class="{k}"/>')
    o.append(text(130, 50, "PreCompact · SessionEnd · Stop", 11, "mut", anchor="middle", mono=True, P=P))

    o.append(path("M1062 99 C 1110 99, 1120 70, 1150 60", P, "green", 1.6, "flow", arrow="green"))
    o.append(path("M1062 99 C 1110 99, 1120 128, 1150 140", P, "rose", 1.6, "flow-slow", arrow="rose"))
    o.append(text(1150, 48, "stored", 12, "green", 700, "end", P=P))
    o.append(text(1150, 160, "kept queued", 12, "rose", 700, "end", P=P))

    checks = ["every file and commit it names must appear in the session",
              "it must be a record of facts, never invented dialogue",
              "anything doubtful waits on disk for a retry",
              "nothing unverified ever reaches the graph"]
    o.append(box(670, 182, 490, 150, P, fill="card2"))
    o.append(label(690, 208, "Verified before it is stored", P, "rose"))
    for i, c in enumerate(checks):
        o.append(dot(694, 233 + i * 26, 3.5, P, "rose"))
        o.append(text(706, 237 + i * 26, c, 13, "fg", P=P))

    o.append(box(40, 182, 590, 150, P, fill="card2"))
    o.append(label(60, 208, "Also searchable, word for word", P, "blue"))
    o.append(text(60, 240, "The raw conversation is indexed verbatim, with", 14, "fg", P=P))
    o.append(text(60, 262, "secrets redacted, so brahmastra_search_sessions", 14, "fg", P=P))
    o.append(text(60, 284, "finds the exact words a summary dropped.", 14, "fg", P=P))
    o.append(text(60, 318, "A missing note is recoverable. A false note is not.", 14, "amber", 700, P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# icons — 24x24 line icons, drawn with the stroke colour of their tile
# --------------------------------------------------------------------------

ICONS = {
    "graph": '<circle cx="5" cy="18" r="2.6"/><circle cx="12" cy="5" r="2.6"/><circle cx="19" cy="16" r="2.6"/>'
             '<path d="M6.5 16 L10.8 7.3 M13.8 6.6 L17.6 13.8 M7.6 18 L16.4 16.3"/>',
    "merge": '<path d="M3 5 L11 12 M3 19 L11 12 M3 12 L20 12 M16 8 L20 12 L16 16"/>',
    "conflict": '<path d="M12 3 L22 20 L2 20 Z"/><path d="M12 9 L12 14"/><circle cx="12" cy="17" r="0.6"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="M14.5 14.5 L21 21"/>',
    "answer": '<path d="M4 4 H20 A1 1 0 0 1 21 5 V15 A1 1 0 0 1 20 16 H10 L5 20 V16 H4 A1 1 0 0 1 3 15 V5 A1 1 0 0 1 4 4 Z"/>'
              '<path d="M8 10 L11 13 L16 7.5"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11 A7 7 0 0 0 19 11 M12 18 V21"/>',
    "history": '<circle cx="12" cy="12" r="9"/><path d="M12 7 V12 L15.5 14"/>',
    "layers": '<path d="M12 3 L21 8 L12 13 L3 8 Z M3 12 L12 17 L21 12 M3 16 L12 21 L21 16"/>',
    "code": '<path d="M8 7 L3 12 L8 17 M16 7 L21 12 L16 17 M14 4 L10 20"/>',
    "note": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8.5 8 H15.5 M8.5 12 H15.5 M8.5 16 H12.5"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11 V8 A4 4 0 0 1 16 8 V11"/>',
    "pulse": '<path d="M2 12 H7 L10 5 L14 19 L17 12 H22"/>',
}


def icon(kind, x, y, P, hue, scale=1.0):
    return (f'<g transform="translate({x} {y}) scale({scale})" fill="none" stroke="{P[hue]}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{ICONS[kind]}</g>')


def tag(x, y, s, P, hue, anchor="start"):
    """A tinted pill with an uppercase mono label."""
    w = int(len(s) * 6.9) + 22
    x0 = x - w if anchor == "end" else x
    return (f'<rect x="{x0}" y="{y}" width="{w}" height="24" rx="12" fill="{P[hue]}" fill-opacity="0.14"/>'
            + text(x0 + w / 2, y + 16, s, 11, hue, 700, "middle", mono=True, spacing="1", P=P)), w


# --------------------------------------------------------------------------
# 9. why — the four promises, as tiles
# --------------------------------------------------------------------------

def why(P):
    mo = Motion(8)
    o = []
    cols = [(150, "amber", "01 · Memory", "Remembers for months",
             ["Every session builds on the last.", "No assistant starts from zero."]),
            (450, "cyan", "02 · Shared", "One brain, every device",
             ["Claude Code, Cursor and your agents", "read and write the same memory."]),
            (750, "green", "03 · Grounded", "Every fact has a source",
             ["Facts keep the sentence they came", "from. Answers cite their notes."]),
            (1050, "rose", "04 · Honest", "Honest about doubt",
             ["Conflicts are caught, the newest wins,", "and the unknown stays “unresolved”."])]
    for cx, hue, eyebrow, title, sub in cols:
        o.append(text(cx - 120, 48, eyebrow.upper(), 12, hue, 700, spacing="1.6", mono=True, P=P))
        o.append(f'<g class="pulse">{dot(cx + 118, 44, 4, P, hue)}</g>')
        o.append(text(cx, 300, title, 22, hue, 700, "middle", P=P))
        o.append(text(cx, 330, sub[0], 14, "mut", anchor="middle", P=P))
        o.append(text(cx, 351, sub[1], 14, "mut", anchor="middle", P=P))

    # 01 memory: a week of sessions settling into one lasting node
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    for i, d in enumerate(days):
        x = 62 + i * 44
        o.append(box(x, 84, 36, 26, P, r=8, fill="card", stroke="amber", sw=1.2, extra=f'opacity="{0.45 + i * 0.13:.2f}"'))
        o.append(text(x + 18, 101, d, 10, "fg", 600, "middle", mono=True, P=P))
        o.append(path(f"M{x + 18} 112 C {x + 18} 160, 150 170, 150 196", P, "amber", 1.4, "flow", opacity=0.75))
    o.append(f'<circle cx="150" cy="214" r="34" fill="{P["amber"]}" fill-opacity="{P["glow"]}" class="breathe"/>')
    o.append(dot(150, 214, 20, P, "amber", "pulse"))
    o.append(icon("graph", 140, 204, P, "bg", 0.85))

    # 02 shared: four devices around one brain
    o.append(f'<circle cx="450" cy="166" r="38" fill="{P["cyan"]}" fill-opacity="{P["glow"]}" class="breathe"/>')
    o.append(dot(450, 166, 20, P, "cyan", "pulse"))
    o.append(icon("graph", 440, 156, P, "bg", 0.85))
    for dx, dy in ((-96, -62), (96, -62), (-96, 62), (96, 62)):
        x, y = 450 + dx, 166 + dy
        o.append(path(f"M{x - dx * 0.25} {y - dy * 0.3} L {450 + dx * 0.3} {166 + dy * 0.3}", P, "cyan", 1.5, "flow"))
        o.append(f'<rect x="{x - 17}" y="{y - 12}" width="34" height="22" rx="4" fill="{P["card"]}" stroke="{P["cyan"]}" stroke-width="1.6"/>')
        o.append(line(x - 21, y + 14, x + 21, y + 14, P, "cyan", 1.6))

    # 03 grounded: a quoted sentence becomes a fact becomes a cited answer
    o.append(box(660, 76, 180, 70, P, r=10))
    for i, wdt in enumerate((140, 110, 128)):
        c = "green" if i == 1 else "line"
        o.append(f'<rect x="676" y="{92 + i * 16}" width="{wdt}" height="6" rx="3" fill="{P[c]}"/>')
    o.append(path("M750 146 L 750 172", P, "green", 1.5, "flow"))
    tp, tw = triple_pill(750 - 92, 174, "Priya", "decided", "Friday", P, "green", 11, 26, 10)
    o.append(tp)
    o.append(path("M750 200 L 750 222", P, "green", 1.5, "flow"))
    o.append(f'<text x="750" y="244" font-size="12" text-anchor="middle" class="m" fill="{P["fg"]}">'
             f'ships Friday <tspan fill="{P["green"]}" font-weight="700">[n:4f2a]</tspan></text>')

    # 04 honest: a conflict resolved, and an unknown left unresolved
    a, aw = pill(988, 86, "May 28", P, "rose", 12, 14, 26)
    b, bw = pill(988, 132, "June 10", P, "green", 12, 14, 26, filled=True)
    o.append(a + f'<line x1="996" y1="99" x2="{988 + aw - 8}" y2="99" stroke="{P["rose"]}" stroke-width="2"/>')
    o.append(b)
    o.append(text(988 + bw + 12, 150, "newest wins", 12, "green", 600, P=P))
    o.append(text(988 + aw + 12, 104, "older", 12, "mut", P=P))
    u, _ = pill(988, 196, "unresolved", P, "mut", 12, 14, 26)
    o.append(u + text(988, 244, "when the notes can't say", 12, "mut", P=P))
    o.append(f'<g class="blink">{dot(1112, 209, 4, P, "rose")}</g>')

    o.append(f'<text x="600" y="408" font-size="15" text-anchor="middle" font-style="italic" class="s" fill="{P["mut"]}">'
             f'Write once. Remember for months. <tspan fill="{P["amber"]}" font-weight="700">Trace every answer.</tspan></text>')
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 10. features — twelve capabilities, one graph
# --------------------------------------------------------------------------

FEATURES = [
    ("graph", "amber", "Knowledge graph", ["Notes become typed facts across", "18 relations, with no manual links."], "AUTO-LINKED"),
    ("merge", "cyan", "Entity resolution", ["Sarah, Sarah K. and Sarah Khan", "become one person, safely."], "3 NAMES → 1"),
    ("conflict", "rose", "Contradiction detection", ["Plans that change are caught,", "and the newest fact wins."], "NEWEST WINS"),
    ("search", "blue", "Hybrid search", ["Full-text and vector search", "fused into one ranking."], "WORDS + MEANING"),
    ("answer", "green", "Graph-first answers", ["Questions answered from the graph,", "multi-hop, with sources."], "CITED"),
    ("mic", "cyan", "Meeting intelligence", ["Decisions, owners, risks and task", "status, from any transcript."], "QUOTED"),
    ("history", "amber", "Session memory", ["AI conversations captured and", "searchable, word for word."], "NEVER LOST"),
    ("code", "green", "Code index", ["Every symbol with its file and", "line range, kept current."], "FILE · LINE"),
    ("layers", "blue", "Workspaces", ["Separate graphs for work, home", "and every project."], "ISOLATED"),
    ("note", "amber", "Notion, both ways", ["Pages sync in; insights are", "written back to each page."], "TWO-WAY SYNC"),
    ("lock", "rose", "Secure by default", ["API key required, secrets", "redacted, keys never exposed."], "LOCKED"),
    ("pulse", "green", "Diagnostics", ["What is waiting, what failed,", "and the button that fixes it."], "SELF-SERVE"),
]


def features(P):
    mo = Motion(8)
    o = [text(600, 46, "TWELVE CAPABILITIES · ONE GRAPH", 12, "mut", 700, "middle", spacing="2.4", P=P)]
    for i, (ic, hue, title, desc, tg) in enumerate(FEATURES):
        col, row = i % 4, i // 4
        x, y = 40 + col * 285, 72 + row * 160
        o.append(box(x, y, 265, 142, P, r=14))
        o.append(f'<circle cx="{x + 32}" cy="{y + 34}" r="20" fill="{P[hue]}" fill-opacity="0.14"/>')
        o.append(icon(ic, x + 20, y + 22, P, hue))
        o.append(f'<g class="pulse" style="animation-delay:{i * 0.21:.2f}s">{dot(x + 245, y + 20, 3.5, P, hue)}</g>')
        o.append(text(x + 64, y + 40, title, 16, "fg", 700, P=P))
        o.append(text(x + 20, y + 78, desc[0], 13, "mut", P=P))
        o.append(text(x + 20, y + 97, desc[1], 13, "mut", P=P))
        o.append(text(x + 20, y + 126, tg, 11, hue, 700, mono=True, spacing="1.4", P=P))
    return mo, "".join(o)


# --------------------------------------------------------------------------
# 11. use-case cards, 600 x 320 each
# --------------------------------------------------------------------------

def card(P, hue, title, sub, em, left_label, left, mid, right_label, right, tags):
    mo = Motion(6)
    o = [box(1, 1, 598, 318, P, r=18, fill="bg", stroke="line")]
    t1, t2 = title.split("|") if "|" in title else (title, "")
    o.append(f'<text x="32" y="60" font-size="28" font-weight="800" class="s" fill="{P[hue]}">{esc(t1)}'
             + (f'<tspan fill="{P["fg"]}">{esc(t2)}</tspan>' if t2 else "") + "</text>")
    o.append(f'<text x="32" y="90" font-size="15" class="s" fill="{P["mut"]}">{esc(sub)} '
             f'<tspan fill="{P[hue]}" font-style="italic" font-weight="600">{esc(em)}</tspan></text>')
    for x, lab, body in ((32, left_label, left), (348, right_label, right)):
        o.append(box(x, 116, 220, 140, P, r=12, fill="card", stroke="line"))
        o.append(text(x + 16, 140, lab.upper(), 10, hue, 700, spacing="1.6", mono=True, P=P))
        o.append(body(x + 16, 164))
    o.append(path("M262 186 L 338 186", P, hue, 1.8, "flow", arrow=hue))
    o.append(text(300, 176, mid.upper(), 10, hue, 700, "middle", spacing="1.4", mono=True, P=P))
    x = 32
    for s in tags:
        t, w = tag(x, 276, s, P, hue)
        o.append(t)
        x += w + 8
    o.append(text(568, 293, "DOCS →", 11, "mut", 700, "end", spacing="1.4", mono=True, P=P))
    return mo, "".join(o)


def lines_mono(rows, P, size=12, gap=21):
    """rows: list of (text, colour) or (speaker, text, colour)."""
    def draw(x, y):
        out = []
        for i, r in enumerate(rows):
            yy = y + i * gap
            if len(r) == 3:
                sp, t, c = r
                out.append(f'<text x="{x}" y="{yy}" font-size="{size}" class="m" fill="{P["fg"]}">'
                           f'<tspan fill="{P[c]}" font-weight="700">{esc(sp)}:</tspan> {esc(t)}</text>')
            else:
                t, c = r
                out.append(text(x, yy, t, size, c, 600 if c != "fg" else 400, mono=True, P=P))
        return "".join(out)
    return draw


def card_shared(P):
    return card(P, "cyan", "Shared team memory", "Every assistant on the team reads", "one memory.",
                "Priya · Claude Code", lines_mono([("add_note(", "fg"), ('  "Export API ships', "fg"),
                                                    ('   Friday, not Monday")', "fg"), ("→ stored, searchable", "green")], P),
                "MCP", "Marco · Cursor", lines_mono([('search_notes(', "fg"), ('  "export API")', "fg"),
                                                     ("→ Friday, not Monday", "green"), ("  decided by Priya", "mut")], P),
                ["MCP", "CLAUDE CODE", "CURSOR"])


def card_meetings(P):
    def graph(x, y):
        cx, cy = x + 94, y + 30
        pts = [(-62, -14, "amber", "D"), (58, -18, "cyan", "A"), (-50, 46, "rose", "!"), (64, 44, "green", "✓")]
        out = [line(cx, cy, cx + dx, cy + dy, P, "mut", 1.2, opacity=0.6) for dx, dy, _, _ in pts]
        out.append(dot(cx, cy, 18, P, "blue", "pulse"))
        out.append(text(cx, cy + 4, "sync", 10, "bg", 700, "middle", mono=True, P=P))
        for i, (dx, dy, c, s) in enumerate(pts):
            out.append(f'<g class="pulse" style="animation-delay:{i * 0.4}s">{dot(cx + dx, cy + dy, 12, P, c)}</g>')
            out.append(text(cx + dx, cy + dy + 4, s, 11, "bg", 800, "middle", P=P))
        return "".join(out)
    return card(P, "amber", "Meetings → |knowledge", "Decisions, owners, risks and tasks,", "each one quoted.",
                "Transcript", lines_mono([("priya", "ship Friday", "amber"), ("raj", "I'll take recon", "cyan"),
                                          ("mei", "staging blocked", "rose"), ("raj", "recon is done", "cyan")], P),
                "Read", "Knowledge graph", graph, ["DECISIONS", "ACTIONS", "RISKS", "STATUS"])


def card_onboarding(P):
    def ans(x, y):
        return (lines_mono([("Notes can't be rebuilt,", "fg"), ("so they live apart", "fg"),
                            ("from the graph.", "fg")], P, 12, 20)(x, y)
                + pill(x, y + 66, "from the graph · [n:4f2a]", P, "green", 10, 8, 20)[0])
    return card(P, "green", "Onboarding", "New hires ask why, and get", "the decision behind it.",
                "New hire asks", lines_mono([("Why do notes live", "fg"), ("in Postgres, not", "fg"),
                                             ("in the graph?", "fg")], P, 13, 22),
                "Ask", "Answer", ans, ["CITED ANSWERS", "GRAPH-FIRST"])


def card_decisions(P):
    def res(x, y):
        return (text(x, y + 14, "June 10", 26, "green", 800, P=P)
                + text(x + 112, y + 12, "✓", 20, "green", 800, P=P)
                + text(x, y + 44, "newest note wins", 12, "fg", 600, P=P)
                + text(x, y + 66, "May 28 kept as evidence", 11, "mut", P=P))
    return card(P, "rose", "Decision log", "When plans change,", "the newest fact wins.",
                "Notes", lines_mono([("2 May", "mut"), ("launch → May 28", "fg"), ("20 May", "mut"),
                                     ("launch → June 10", "fg")], P),
                "Resolve", "The graph says", res, ["CONTRADICTIONS", "HISTORY"])


def card_code(P):
    return card(P, "blue", "Codebase memory", "Your agent knows where code lives", "and why it changed.",
                "search_code", lines_mono([('"where are keys', "fg"), (' rotated?"', "fg"),
                                           ("→ groq_pool.py", "green"), ("  lines 41-88", "mut")], P),
                "And", "search_sessions", lines_mono([('"how did we fix', "fg"), (' the leak?"', "fg"),
                                                        ("→ the exact words", "green"), ("  from last Tuesday", "mut")], P),
                ["CODE INDEX", "SESSIONS", "REDACTED"])


def card_notion(P):
    def ins(x, y):
        return (text(x, y, "▾ Brahmastra Insights", 12, "fg", 700, P=P)
                + lines_mono([("  relationships", "fg"), ("  contradictions", "rose"), ("  suggested links", "cyan")],
                             P, 12, 21)(x, y + 24))
    def page(x, y):
        bars = "".join(f'<rect x="{x}" y="{y + 24 + i * 16}" width="{w}" height="6" rx="3" fill="{P["line"]}"/>'
                       for i, w in enumerate((176, 150, 168, 120)))
        return text(x, y + 4, "Q3 launch plan", 14, "fg", 700, P=P) + bars
    return card(P, "amber", "Notion, |with a brain", "Write in Notion;", "insights write themselves back.",
                "Notion page", page, "Sync", "Written back", ins, ["TWO-WAY SYNC", "NO [[LINKS]]"])


CARDS = [("card-shared", card_shared, "Shared team memory"), ("card-meetings", card_meetings, "Meetings to knowledge"),
         ("card-onboarding", card_onboarding, "Onboarding"), ("card-decisions", card_decisions, "Decision log"),
         ("card-code", card_code, "Codebase memory"), ("card-notion", card_notion, "Notion with a brain")]


DIAGRAMS = [
    ("hero", 1200, 590, "Brahmastra: a shared brain for every AI",
     "Notes, meetings, AI coding sessions, code and any agent feed one Brahmastra knowledge graph. "
     "Claude Code, Cursor, any MCP client and your own agents read and write it over MCP, on any device.", hero),
    ("pipeline", 1200, 470, "A note's journey through the pipeline",
     "A standup note passes through seven incremental stages: sync, extract, resolve, build graph, "
     "summarise, cache and write back. It becomes three quoted facts, one resolved person, a graph and a topic summary.", pipeline),
    ("resolution", 1200, 350, "Entity resolution",
     "Sarah, Sarah K. and Sarah Khan merge into one entity. Brahmastra v3 versus Brahmastra, an activity "
     "versus its object, and the same name in two workspaces are refused.", resolution),
    ("contradiction", 1200, 360, "Contradictions resolve to the newest note",
     "A planning doc says the launch is May 28; a later standup says June 10. scheduled_for holds one value, "
     "so the conflict is detected and the newer note wins.", contradiction),
    ("meetings", 1200, 570, "A meeting becomes graph knowledge",
     "Quoted transcript lines become a decision decided by Priya, an action item assigned to Raj and "
     "later marked done from his own words, and a risk raised by Mei, all discussed in the meeting.", meetings),
    ("ask", 1200, 400, "Graph-first answers",
     "A question is answered from the graph first, by entity name and statement meaning. Only if the graph "
     "has nothing are raw transcript passages read, and the answer is marked as coming from the transcript.", ask),
    ("storage", 1200, 400, "Source data versus derived data",
     "Notes and workspaces live in Postgres and cannot be recomputed. The graph in Neo4j is a cache the "
     "pipeline rebuilds from the notes. Every derived row has an owner.", storage),
    ("checkpoint", 1200, 350, "Session checkpoints",
     "An AI session is captured to disk by a hook that cannot block, distilled by a model, and checked; "
     "a doubtful note is rejected and the capture stays queued. The raw conversation is indexed verbatim alongside.", checkpoint),
]


DIAGRAMS += [
    ("why", 1200, 440, "Why Brahmastra",
     "Four promises: it remembers for months, one brain is shared by every device, every fact has a "
     "source, and it is honest about what it does not know.", why),
    ("features", 1200, 552, "Twelve capabilities, one graph",
     "Knowledge graph, entity resolution, contradiction detection, hybrid search, graph-first answers, "
     "meeting intelligence, session memory, code index, workspaces, Notion sync, secure defaults and diagnostics.",
     features),
] + [(name, 600, 320, title, f"Use case: {title}.", fn) for name, fn, title in CARDS]


if __name__ == "__main__":
    for name, w, h, title, desc, fn in DIAGRAMS:
        write(name, w, h, title, desc, fn)
        print(f"wrote {name}-light.svg, {name}-dark.svg")
