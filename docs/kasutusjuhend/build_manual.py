"""Build the Estonian Seadusloome user manual PDF.

The repository cannot rely on a running local Postgres/Jena stack for
manual screenshots, so this script creates faithful, implementation-based
screen-view illustrations and workflow diagrams as static assets, then
prints a self-contained HTML manual to PDF with headless Chrome.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
HTML_PATH = ROOT / "seadusloome_kasutusjuhend.html"
PDF_PATH = ROOT / "Seadusloome_kasutusjuhend.pdf"

CHROME_PATHS = [
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
]

COLORS = {
    "bg": "#0F172A",
    "surface": "#1E293B",
    "raised": "#293548",
    "border": "#3D4B5E",
    "border_strong": "#64748B",
    "text": "#FFFFFF",
    "muted": "#CBD5E1",
    "primary": "#00C3FF",
    "primary_dark": "#0062F5",
    "blue": "#0030DE",
    "success": "#22C55E",
    "warning": "#FBBF24",
    "danger": "#EF4444",
    "purple": "#A78BFA",
    "orange": "#FB923C",
    "green": "#34D399",
    "pink": "#F472B6",
}


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Verdana.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


FONTS = {
    "xl": font(44, True),
    "h1": font(34, True),
    "h2": font(26, True),
    "h3": font(22, True),
    "body": font(20),
    "body_bold": font(20, True),
    "small": font(16),
    "small_bold": font(16, True),
    "tiny": font(13),
}


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill: str = "text",
    f: ImageFont.ImageFont | None = None,
) -> None:
    draw.text(xy, text, fill=hex_to_rgb(COLORS[fill]), font=f or FONTS["body"])


def text_size(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=f)
    return box[2] - box[0], box[3] - box[1]


def rounded(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: str,
    outline: str | None = None,
    radius: int = 16,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(
        box,
        radius=radius,
        fill=hex_to_rgb(COLORS[fill]),
        outline=hex_to_rgb(COLORS[outline]) if outline else None,
        width=width,
    )


def pill(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    fill: str = "primary",
    text_color: str = "bg",
    pad_x: int = 18,
    pad_y: int = 8,
) -> tuple[int, int, int, int]:
    w, h = text_size(draw, text, FONTS["small_bold"])
    x, y = xy
    box = (x, y, x + w + pad_x * 2, y + h + pad_y * 2)
    draw.rounded_rectangle(box, radius=999, fill=hex_to_rgb(COLORS[fill]))
    draw.text(
        (x + pad_x, y + pad_y - 1),
        text,
        fill=hex_to_rgb(COLORS[text_color]),
        font=FONTS["small_bold"],
    )
    return box


def wrap_lines(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False, replace_whitespace=False)


def paragraph(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    width_chars: int = 60,
    line_height: int = 28,
    fill: str = "muted",
    f: ImageFont.ImageFont | None = None,
) -> int:
    x, y = xy
    for line in wrap_lines(text, width_chars):
        draw.text((x, y), line, fill=hex_to_rgb(COLORS[fill]), font=f or FONTS["body"])
        y += line_height
    return y


def base_screen(title: str, active: str) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (1600, 980), hex_to_rgb(COLORS["bg"]))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, 1600, 78), fill="surface", outline="border", radius=0)
    draw_text(draw, (34, 25), "Seadusloome", fill="primary", f=FONTS["h3"])
    draw_text(draw, (1320, 26), "Mari Mets", fill="text", f=FONTS["small_bold"])
    draw.ellipse((1468, 20, 1514, 66), fill=hex_to_rgb(COLORS["primary"]))
    draw_text(draw, (1481, 34), "MM", fill="bg", f=FONTS["tiny"])

    rounded(draw, (0, 78, 250, 980), fill="surface", outline="border", radius=0)
    nav = ["Töölaud", "Uurija", "Eelnõud", "Koostaja", "Vestlus", "Kasutajad"]
    y = 126
    for item in nav:
        if item == active:
            rounded(draw, (24, y - 10, 226, y + 42), fill="raised", outline="primary", radius=10)
            fill = "text"
        else:
            fill = "muted"
        draw_text(
            draw, (52, y + 2), item, fill=fill, f=FONTS["body_bold" if item == active else "body"]
        )
        y += 62

    draw_text(draw, (292, 118), title, fill="text", f=FONTS["h1"])
    return img, draw


def screenshot_dashboard() -> None:
    img, draw = base_screen("Tere tulemast, Mari Mets!", "Töölaud")
    y = 180
    rounded(draw, (292, y, 1518, y + 110), fill="raised", outline="border", radius=14)
    draw_text(draw, (322, y + 25), "Tööpäeva kokkuvõte", fill="text", f=FONTS["h3"])
    paragraph(
        draw,
        (322, y + 60),
        "Teil on 4 eelnõu, 2 koostaja sessiooni ja 6 vestlust. Kiirlingid viivad pooleliolevate tööde juurde.",
        width_chars=92,
        line_height=26,
    )
    cards = [
        ("Eelnõud", "4", "2 mõjuanalüüsi valmis"),
        ("Koostaja", "2", "üks VTK ootab ülevaatust"),
        ("Vestlused", "6", "3 kinnitatud vestlust"),
    ]
    x = 292
    for label, number, note in cards:
        rounded(draw, (x, 330, x + 382, 520), fill="surface", outline="border", radius=14)
        draw_text(draw, (x + 28, 360), label, fill="muted", f=FONTS["small_bold"])
        draw_text(draw, (x + 28, 395), number, fill="primary", f=FONTS["xl"])
        draw_text(draw, (x + 28, 470), note, fill="muted", f=FONTS["small"])
        x += 422
    rounded(draw, (292, 560, 900, 908), fill="surface", outline="border", radius=14)
    draw_text(draw, (322, 592), "Viimased tegevused", fill="text", f=FONTS["h3"])
    rows = [
        ("10:42", "Mõjuanalüüs valmis: Avaliku sektori AI eelnõu"),
        ("09:20", "Vestlus seotud eelnõuga: andmekaitse sätted"),
        ("Eile", "Lisatud järjehoidja: KarS § 133"),
        ("Eile", "Koostaja lõi VTK eelnõu struktuuri"),
    ]
    yy = 648
    for when, activity in rows:
        draw_text(draw, (322, yy), when, fill="primary", f=FONTS["small_bold"])
        draw_text(draw, (398, yy), activity, fill="muted", f=FONTS["small"])
        yy += 54
    rounded(draw, (940, 560, 1518, 908), fill="surface", outline="border", radius=14)
    draw_text(draw, (970, 592), "Järjehoidjad", fill="text", f=FONTS["h3"])
    for yy, item in zip(
        [650, 715, 780],
        [
            "Isikuandmete kaitse üldmäärus",
            "Avaliku teabe seadus § 35",
            "Riigikohtu otsus 3-1-1-12-21",
        ],
        strict=False,
    ):
        rounded(draw, (970, yy - 16, 1478, yy + 30), fill="raised", outline="border", radius=10)
        draw_text(draw, (990, yy - 4), item, fill="muted", f=FONTS["small"])
    img.save(ASSETS / "screenshot_dashboard.png", quality=95)


def screenshot_explorer() -> None:
    img = Image.new("RGB", (1600, 980), hex_to_rgb(COLORS["bg"]))
    draw = ImageDraw.Draw(img)
    rounded(draw, (0, 0, 1600, 86), fill="surface", outline="border", radius=0)
    draw_text(draw, (34, 24), "Eesti õiguse ontoloogia", fill="text", f=FONTS["h2"])
    pill(draw, (372, 24), "Uurija", fill="primary")
    pill(draw, (476, 24), "D3.js", fill="purple", text_color="text")
    rounded(draw, (700, 18, 1192, 66), fill="bg", outline="border", radius=10)
    draw_text(draw, (724, 32), "Otsi seadust, eelnõu, lahendit...", fill="muted", f=FONTS["small"])
    rounded(draw, (1208, 18, 1306, 66), fill="primary", radius=10)
    draw_text(draw, (1237, 32), "Otsi", fill="bg", f=FONTS["small_bold"])
    rounded(draw, (0, 86, 1600, 144), fill="raised", outline="border", radius=0)
    for x, label in zip(
        [34, 250, 390, 585, 730, 860],
        ["Taaskäivita", "Sildid", "Rühmita", "Lähtesta", "Ülevaade", "Eelnõud"],
        strict=False,
    ):
        rounded(draw, (x, 101, x + 140, 132), fill="surface", outline="border", radius=8)
        draw_text(draw, (x + 17, 107), label, fill="muted", f=FONTS["tiny"])

    # graph area
    nodes = {
        "AI eelnõu": (650, 420, "purple"),
        "IKS": (480, 310, "primary"),
        "AvTS": (520, 560, "primary"),
        "GDPR": (820, 285, "green"),
        "Riigikohus": (920, 540, "orange"),
        "KarS": (390, 720, "primary"),
        "EL AI määrus": (1030, 385, "green"),
    }
    links = [
        ("AI eelnõu", "IKS"),
        ("AI eelnõu", "AvTS"),
        ("AI eelnõu", "GDPR"),
        ("AI eelnõu", "Riigikohus"),
        ("AI eelnõu", "EL AI määrus"),
        ("AvTS", "KarS"),
        ("GDPR", "EL AI määrus"),
    ]
    for a, b in links:
        ax, ay, _ = nodes[a]
        bx, by, _ = nodes[b]
        draw.line((ax, ay, bx, by), fill=hex_to_rgb(COLORS["border_strong"]), width=3)
    for label, (x, y, color) in nodes.items():
        r = 34 if label == "AI eelnõu" else 28
        draw.ellipse(
            (x - r, y - r, x + r, y + r),
            fill=hex_to_rgb(COLORS[color]),
            outline=hex_to_rgb(COLORS["text"]),
            width=2,
        )
        draw_text(draw, (x - 56, y + r + 12), label, fill="text", f=FONTS["small_bold"])

    rounded(draw, (34, 190, 300, 500), fill="surface", outline="border", radius=14)
    draw_text(draw, (64, 220), "Kategooriad", fill="text", f=FONTS["h3"])
    legend = [
        ("Kehtiv seadus", "primary"),
        ("Eelnõu", "purple"),
        ("Kohtulahend", "orange"),
        ("EL õigusakt", "green"),
    ]
    yy = 275
    for label, color in legend:
        draw.ellipse((64, yy, 84, yy + 20), fill=hex_to_rgb(COLORS[color]))
        draw_text(draw, (100, yy - 3), label, fill="muted", f=FONTS["small"])
        yy += 44

    rounded(draw, (1240, 170, 1540, 870), fill="surface", outline="border", radius=14)
    draw_text(draw, (1270, 205), "Detailpaneel", fill="text", f=FONTS["h3"])
    draw_text(draw, (1270, 252), "Avaliku sektori AI eelnõu", fill="primary", f=FONTS["body_bold"])
    paragraph(
        draw,
        (1270, 296),
        "Mõjutab isikuandmete töötlemist, avaliku teabe avalikustamist ja EL AI määruse täitmist.",
        width_chars=29,
        line_height=28,
    )
    draw_text(draw, (1270, 425), "Seosed", fill="text", f=FONTS["small_bold"])
    for yy, item in zip(
        [466, 510, 554, 598], ["IKS § 10", "AvTS § 35", "GDPR art 6", "EL AI määrus"], strict=False
    ):
        rounded(draw, (1270, yy, 1504, yy + 34), fill="raised", outline="border", radius=8)
        draw_text(draw, (1290, yy + 8), item, fill="muted", f=FONTS["tiny"])
    rounded(draw, (1270, 760, 1504, 812), fill="primary", radius=10)
    draw_text(draw, (1302, 776), "Lisa järjehoidjatesse", fill="bg", f=FONTS["small_bold"])
    img.save(ASSETS / "screenshot_explorer.png", quality=95)


def screenshot_drafts() -> None:
    img, draw = base_screen("Eelnõud", "Eelnõud")
    rounded(draw, (292, 174, 1518, 282), fill="raised", outline="border", radius=14)
    paragraph(
        draw,
        (322, 202),
        "Organisatsiooni eelnõude töölaud. Laadige üles .docx või .pdf, jälgige töötlemist ning avage valmis mõjuaruanne.",
        width_chars=92,
        line_height=28,
    )
    rounded(draw, (1290, 198, 1490, 250), fill="primary", radius=10)
    draw_text(draw, (1318, 214), "Laadi üles uus", fill="bg", f=FONTS["small_bold"])

    rounded(draw, (292, 320, 1518, 888), fill="surface", outline="border", radius=14)
    draw_text(draw, (322, 356), "Minu organisatsiooni eelnõud", fill="text", f=FONTS["h3"])
    filters = [
        ("Otsi...", 322, 408, 500),
        ("Tüüp", 848, 408, 160),
        ("Staatus", 1030, 408, 170),
        ("Üleslaadija", 1222, 408, 190),
    ]
    for label, x, y, w in filters:
        rounded(draw, (x, y, x + w, y + 48), fill="bg", outline="border", radius=8)
        draw_text(draw, (x + 18, y + 14), label, fill="muted", f=FONTS["small"])

    columns = ["Pealkiri", "Tüüp", "Staatus", "Üleslaadija", "Viimati muudetud"]
    xs = [322, 760, 900, 1100, 1290]
    for x, col in zip(xs, columns, strict=False):
        draw_text(draw, (x, 500), col, fill="text", f=FONTS["small_bold"])
    draw.line((322, 536, 1486, 536), fill=hex_to_rgb(COLORS["border"]), width=2)
    rows = [
        ("Avaliku sektori AI eelnõu", "Eelnõu", "Valmis", "Mari Mets", "30.04.2026"),
        ("Andmevahetuse VTK", "VTK", "Mõjude analüüs", "Kaur Saar", "30.04.2026"),
        ("Küberturvalisuse muudatused", "Eelnõu", "Olemite eraldamine", "Mari Mets", "29.04.2026"),
        ("Avaliku teabe seaduse täpsustus", "Eelnõu", "Üles laaditud", "Liis Tamm", "28.04.2026"),
    ]
    yy = 570
    for title, typ, status, user, date in rows:
        draw_text(draw, (322, yy), title, fill="primary", f=FONTS["small_bold"])
        draw_text(draw, (760, yy), typ, fill="muted", f=FONTS["small"])
        status_color = (
            "success"
            if status == "Valmis"
            else "warning"
            if status == "Mõjude analüüs"
            else "primary"
            if status == "Olemite eraldamine"
            else "border_strong"
        )
        pill(
            draw,
            (900, yy - 8),
            status,
            fill=status_color,
            text_color="bg" if status_color in {"success", "warning", "primary"} else "text",
            pad_x=12,
            pad_y=6,
        )
        draw_text(draw, (1100, yy), user, fill="muted", f=FONTS["small"])
        draw_text(draw, (1290, yy), date, fill="muted", f=FONTS["small"])
        yy += 72

    rounded(draw, (322, 810, 1486, 850), fill="raised", outline="border", radius=10)
    steps = ["Üles laaditud", "Töötlemine", "Olemite eraldamine", "Mõjude analüüs", "Valmis"]
    x = 350
    for i, step in enumerate(steps):
        color = "success" if i < 2 else "primary" if i == 2 else "border_strong"
        draw.ellipse((x, 822, x + 18, 840), fill=hex_to_rgb(COLORS[color]))
        draw_text(draw, (x + 28, 818), step, fill="muted", f=FONTS["tiny"])
        x += 220
    img.save(ASSETS / "screenshot_drafts.png", quality=95)


def screenshot_chat() -> None:
    img, draw = base_screen("AI nõustaja: avaliku sektori AI", "Vestlus")
    rounded(draw, (292, 170, 1518, 900), fill="surface", outline="border", radius=14)
    pill(draw, (322, 205), "Ühendatud", fill="success")
    draw_text(
        draw,
        (460, 214),
        "Kuutarbimine: 18% organisatsiooni limiidist",
        fill="muted",
        f=FONTS["small"],
    )
    rounded(draw, (322, 282, 870, 390), fill="raised", outline="border", radius=16)
    paragraph(
        draw,
        (352, 314),
        "Milliseid kehtivaid sätteid puudutab eelnõu, mis reguleerib tehisintellekti kasutamist avalikus sektoris?",
        width_chars=52,
        line_height=28,
        fill="text",
    )
    rounded(draw, (530, 430, 1468, 632), fill="bg", outline="border", radius=16)
    paragraph(
        draw,
        (560, 462),
        "Esialgse analüüsi järgi puudutab kavatsus vähemalt isikuandmete kaitse üldmääruse artiklit 6, avaliku teabe seaduse § 35 andmekoosseise ning avaliku sektori andmekogude töötlemise aluseid. Kontrollida tuleks ka EL AI määruse riskikategooriaid.",
        width_chars=80,
        line_height=30,
        fill="muted",
    )
    draw_text(
        draw,
        (560, 575),
        "Allikad: GDPR art 6 · AvTS § 35 · EL AI määrus",
        fill="primary",
        f=FONTS["small_bold"],
    )
    rounded(draw, (322, 690, 1468, 822), fill="raised", outline="border", radius=14)
    draw_text(
        draw, (350, 722), "Küsi Eesti õiguse kohta...  / = käsud", fill="muted", f=FONTS["body"]
    )
    rounded(draw, (1318, 748, 1438, 800), fill="primary", radius=10)
    draw_text(draw, (1352, 765), "Saada", fill="bg", f=FONTS["small_bold"])
    img.save(ASSETS / "screenshot_chat.png", quality=95)


def screenshot_drafter() -> None:
    img, draw = base_screen("Kavatsuse kirjeldamine", "Koostaja")
    steps = ["Kavatsus", "Täpsustus", "Uurimine", "Struktuur", "Sätted", "Ülevaade", "Eksport"]
    x = 310
    for i, step in enumerate(steps):
        color = "primary" if i == 0 else "border_strong"
        draw.ellipse((x, 184, x + 30, 214), fill=hex_to_rgb(COLORS[color]))
        draw_text(
            draw,
            (x + 42, 187),
            step,
            fill="text" if i == 0 else "muted",
            f=FONTS["small_bold" if i == 0 else "small"],
        )
        if i < len(steps) - 1:
            draw.line(
                (x + 140, 199, x + 186, 199), fill=hex_to_rgb(COLORS["border_strong"]), width=3
            )
        x += 172
    rounded(draw, (292, 260, 1518, 360), fill="raised", outline="border", radius=14)
    paragraph(
        draw,
        (322, 288),
        "Kirjeldage seadusandlikku kavatsust vabas vormis. Süsteem esitab täpsustavad küsimused, uurib ontoloogiat ning loob eelnõu struktuuri.",
        width_chars=92,
    )
    rounded(draw, (292, 402, 1518, 842), fill="surface", outline="border", radius=14)
    draw_text(draw, (322, 438), "1. samm: Kavatsus", fill="text", f=FONTS["h3"])
    rounded(draw, (322, 500, 1486, 686), fill="bg", outline="border", radius=12)
    paragraph(
        draw,
        (350, 532),
        "Soovin koostada seaduse, mis reguleerib tehisintellekti kasutamist avalikus sektoris, sealhulgas andmekaitse nõudeid, läbipaistvuse kohustust ja inimjärelevalvet.",
        width_chars=95,
        line_height=32,
        fill="text",
    )
    draw_text(draw, (350, 716), "Kuni 4000 tähemärki.", fill="muted", f=FONTS["small"])
    rounded(draw, (322, 760, 580, 812), fill="primary", radius=10)
    draw_text(draw, (352, 776), "Jätka täpsustamisega", fill="bg", f=FONTS["small_bold"])
    img.save(ASSETS / "screenshot_drafter.png", quality=95)


def write_svg_assets() -> None:
    dataflow = """<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="470" viewBox="0 0 1400 470">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="#00C3FF"/>
    </marker>
    <style>
      text{font-family:Arial,Verdana,sans-serif;fill:#fff}
      .muted{fill:#CBD5E1;font-size:25px}
      .title{font-weight:700;font-size:31px}
      .box{fill:#1E293B;stroke:#3D4B5E;stroke-width:3;rx:18}
      .accent{fill:#293548;stroke:#00C3FF;stroke-width:3;rx:18}
      .line{stroke:#00C3FF;stroke-width:4;fill:none;marker-end:url(#arrow)}
    </style>
  </defs>
  <rect width="1400" height="470" fill="#0F172A" rx="22"/>
  <rect class="box" x="50" y="70" width="255" height="115"/>
  <text class="title" x="84" y="121">Õigusallikad</text>
  <text class="muted" x="84" y="158">RT, EIS, kohtud, EL</text>
  <path class="line" d="M305 128 L420 128"/>
  <rect class="box" x="420" y="70" width="250" height="115"/>
  <text class="title" x="455" y="121">Sünkroonimine</text>
  <text class="muted" x="455" y="158">JSON-LD → RDF</text>
  <path class="line" d="M670 128 L790 128"/>
  <rect class="accent" x="790" y="70" width="255" height="115"/>
  <text class="title" x="828" y="121">Jena Fuseki</text>
  <text class="muted" x="828" y="158">SPARQL-ontoloogia</text>
  <path class="line" d="M1045 128 L1165 128"/>
  <rect class="box" x="1165" y="70" width="185" height="115"/>
  <text class="title" x="1195" y="121">Uurija</text>
  <text class="muted" x="1195" y="158">Graaf + detailid</text>
  <rect class="box" x="155" y="290" width="255" height="115"/>
  <text class="title" x="190" y="341">Kasutaja eelnõu</text>
  <text class="muted" x="190" y="378">DOCX/PDF või intent</text>
  <path class="line" d="M410 348 L520 348"/>
  <rect class="box" x="520" y="290" width="250" height="115"/>
  <text class="title" x="555" y="341">Analüüs</text>
  <text class="muted" x="555" y="378">Tika, LLM, SPARQL</text>
  <path class="line" d="M770 348 L890 348"/>
  <rect class="accent" x="890" y="290" width="260" height="115"/>
  <text class="title" x="930" y="341">Tulemid</text>
  <text class="muted" x="930" y="378">mõju, konfliktid, raport</text>
  <path class="line" d="M1015 290 C1005 245 970 218 930 185"/>
  <path class="line" d="M1150 348 L1250 348"/>
  <rect class="box" x="1250" y="290" width="100" height="115"/>
  <text class="title" x="1272" y="341">AI</text>
  <text class="muted" x="1272" y="378">vestlus</text>
</svg>"""

    workflow = """<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="420" viewBox="0 0 1400 420">
  <defs>
    <marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth">
      <path d="M0,0 L0,6 L9,3 z" fill="#00C3FF"/>
    </marker>
    <style>
      text{font-family:Arial,Verdana,sans-serif;fill:#fff}
      .label{font-weight:700;font-size:28px}
      .small{fill:#CBD5E1;font-size:22px}
      .box{fill:#1E293B;stroke:#3D4B5E;stroke-width:3;rx:18}
      .active{fill:#293548;stroke:#00C3FF;stroke-width:3;rx:18}
      .line{stroke:#00C3FF;stroke-width:4;fill:none;marker-end:url(#arrow)}
    </style>
  </defs>
  <rect width="1400" height="420" fill="#0F172A" rx="22"/>
  <rect class="active" x="55" y="85" width="250" height="110"/><text class="label" x="93" y="133">1. Sisend</text><text class="small" x="93" y="168">eelnõu või kavatsus</text>
  <path class="line" d="M305 140 L400 140"/>
  <rect class="box" x="400" y="85" width="250" height="110"/><text class="label" x="438" y="133">2. Analüüs</text><text class="small" x="438" y="168">viited, mõjud, riskid</text>
  <path class="line" d="M650 140 L745 140"/>
  <rect class="box" x="745" y="85" width="250" height="110"/><text class="label" x="783" y="133">3. Tõlgendus</text><text class="small" x="783" y="168">graaf, vestlus, raport</text>
  <path class="line" d="M995 140 L1090 140"/>
  <rect class="box" x="1090" y="85" width="255" height="110"/><text class="label" x="1126" y="133">4. Otsus</text><text class="small" x="1126" y="168">parandus või eksport</text>
  <rect class="box" x="195" y="265" width="1010" height="70"/>
  <text class="small" x="235" y="310">Tüüpiline ring kordub: ametnik täiendab eelnõu, käivitab uue analüüsi ja võrdleb muutusi.</text>
</svg>"""

    drafter = """<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="360" viewBox="0 0 1400 360">
  <style>
    text{font-family:Arial,Verdana,sans-serif;fill:#fff}
    .step{fill:#1E293B;stroke:#3D4B5E;stroke-width:3;rx:18}
    .num{fill:#00C3FF;font-weight:700;font-size:34px}
    .label{font-weight:700;font-size:23px}
    .small{fill:#CBD5E1;font-size:18px}
    .line{stroke:#64748B;stroke-width:4}
  </style>
  <rect width="1400" height="360" fill="#0F172A" rx="22"/>
  <line class="line" x1="140" y1="180" x2="1260" y2="180"/>
  <g transform="translate(45 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">1</text><text class="label" x="22" y="84">Kavatsus</text><text class="small" x="22" y="116">eesmärk ja piirid</text></g>
  <g transform="translate(235 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">2</text><text class="label" x="22" y="84">Täpsustus</text><text class="small" x="22" y="116">küsimused</text></g>
  <g transform="translate(425 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">3</text><text class="label" x="22" y="84">Uurimine</text><text class="small" x="22" y="116">ontoloogia</text></g>
  <g transform="translate(615 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">4</text><text class="label" x="22" y="84">Struktuur</text><text class="small" x="22" y="116">peatükid ja sätted</text></g>
  <g transform="translate(805 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">5</text><text class="label" x="22" y="84">Tekst</text><text class="small" x="22" y="116">sätete mustand</text></g>
  <g transform="translate(995 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">6</text><text class="label" x="22" y="84">Ülevaade</text><text class="small" x="22" y="116">mõjuanalüüs</text></g>
  <g transform="translate(1185 85)"><rect class="step" width="160" height="150"/><text class="num" x="22" y="48">7</text><text class="label" x="22" y="84">Eksport</text><text class="small" x="22" y="116">DOCX</text></g>
</svg>"""

    (ASSETS / "diagram_dataflow.svg").write_text(dataflow, encoding="utf-8")
    (ASSETS / "diagram_workflow.svg").write_text(workflow, encoding="utf-8")
    (ASSETS / "diagram_drafter_steps.svg").write_text(drafter, encoding="utf-8")


def write_manual_html() -> None:
    html = """<!doctype html>
<html lang="et">
<head>
  <meta charset="utf-8">
  <title>Seadusloome kasutusjuhend</title>
  <style>
    :root {
      --bg: #0F172A;
      --surface: #1E293B;
      --raised: #293548;
      --border: #3D4B5E;
      --text: #FFFFFF;
      --muted: #CBD5E1;
      --primary: #00C3FF;
      --blue: #0030DE;
      --green: #34D399;
      --warning: #FBBF24;
      --danger: #EF4444;
    }
    @page { size: A4; margin: 15mm 14mm 16mm; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Verdana, sans-serif;
      color: #111827;
      background: #ffffff;
      font-size: 11.2pt;
      line-height: 1.45;
    }
    h1, h2, h3 { line-height: 1.18; margin: 0 0 9pt; color: #0F172A; }
    h1 { font-size: 32pt; }
    h2 { font-size: 20pt; border-bottom: 2px solid #E5E7EB; padding-bottom: 5pt; margin-top: 18pt; }
    h3 { font-size: 14pt; margin-top: 12pt; }
    p { margin: 0 0 8pt; }
    ul, ol { margin: 0 0 8pt 18pt; padding: 0; }
    li { margin: 2pt 0; }
    table { width: 100%; border-collapse: collapse; margin: 7pt 0 11pt; font-size: 9.5pt; }
    th, td { border: 1px solid #D1D5DB; padding: 6pt; vertical-align: top; }
    th { background: #EAF7FF; text-align: left; color: #0F172A; }
    .cover {
      min-height: 255mm;
      background: linear-gradient(135deg, #0F172A, #0030DE 60%, #00C3FF);
      color: white;
      margin: -15mm -14mm -16mm;
      padding: 36mm 26mm;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .cover h1 { color: white; font-size: 39pt; margin-bottom: 8pt; }
    .cover .subtitle { font-size: 17pt; max-width: 150mm; color: #E0F2FE; }
    .cover .meta { color: #E0F2FE; font-size: 11pt; }
    .cover .tagline {
      margin-top: 26mm;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8pt;
    }
    .cover-card {
      border: 1px solid rgba(255,255,255,0.35);
      border-radius: 8px;
      padding: 12pt;
      background: rgba(15,23,42,0.32);
      min-height: 32mm;
    }
    .cover-card strong { display:block; font-size: 13pt; margin-bottom: 4pt; }
    .page-break { break-before: page; }
    .avoid-break { break-inside: avoid; }
    .lead { font-size: 12.6pt; color: #374151; }
    .note {
      border-left: 4px solid var(--primary);
      background: #EFF6FF;
      padding: 9pt 11pt;
      margin: 10pt 0;
      break-inside: avoid;
    }
    .warning {
      border-left-color: var(--warning);
      background: #FFFBEB;
    }
    .grid-2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10pt;
      margin: 8pt 0 11pt;
    }
    .card {
      border: 1px solid #D1D5DB;
      border-radius: 8px;
      padding: 10pt;
      break-inside: avoid;
      background: #FFFFFF;
    }
    .card strong { color: #0F172A; }
    .screen, .diagram {
      width: 100%;
      border: 1px solid #D1D5DB;
      border-radius: 8px;
      margin: 7pt 0 3pt;
      display: block;
      break-inside: avoid;
    }
    figure { margin: 10pt 0 12pt; break-inside: avoid; }
    figcaption { font-size: 9pt; color: #4B5563; margin-top: 4pt; }
    .toc {
      columns: 2;
      column-gap: 22pt;
      font-size: 10.5pt;
    }
    .toc p { break-inside: avoid; margin-bottom: 6pt; }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 2pt 7pt;
      background: #E0F2FE;
      color: #075985;
      font-weight: 700;
      font-size: 9pt;
    }
    .muted { color: #4B5563; }
    .footer-note { color: #4B5563; font-size: 9.4pt; }
  </style>
</head>
<body>
  <section class="cover">
    <div>
      <p class="meta">Seadusloome kasutusjuhend · 30. aprill 2026</p>
      <h1>Seadusloome</h1>
      <p class="subtitle">Eesti õigusontoloogia nõustamistarkvara ametnikele, kes kavandavad, analüüsivad ja koostavad õigusakte.</p>
      <div class="tagline">
        <div class="cover-card"><strong>Mõista seoseid</strong>Graaf näitab, milliseid seadusi, sätteid, kohtuotsuseid ja EL allikaid kavatsus puudutab.</div>
        <div class="cover-card"><strong>Hinda mõju</strong>Üleslaetud eelnõust leitakse viited, võimalikud konfliktid, lüngad ja vastavusriskid.</div>
        <div class="cover-card"><strong>Koosta kiiremini</strong>AI vestlus ja koostaja aitavad kavatsuse muuta kontrollitavaks eelnõu mustandiks.</div>
      </div>
    </div>
    <p class="meta">Kasutamiseks ministeeriumi või asutuse sisetöös. Süsteem toetab juristi otsust, kuid ei asenda õiguslikku vastutust ega lõplikku ekspertiisi.</p>
  </section>

  <section class="page-break">
    <h2>Sisukord</h2>
    <div class="toc">
      <p>1. Mis on Seadusloome?</p>
      <p>2. Miks seda vaja on?</p>
      <p>3. Põhimõte ja töövoog</p>
      <p>4. Töölaud ja navigeerimine</p>
      <p>5. Eelnõud ja mõjuanalüüs</p>
      <p>6. Ontoloogia uurija</p>
      <p>7. AI vestlus</p>
      <p>8. AI koostaja</p>
      <p>9. Kasutuslood ja näited</p>
      <p>10. Turvalisus ja head tavad</p>
    </div>

    <h2>1. Mis on Seadusloome?</h2>
    <p class="lead">Seadusloome on nõustamistarkvara, mis aitab ametnikul näha kavandatava õigusakti mõju Eesti ja Euroopa õigusruumis enne, kui tekst liigub järgmisse kooskõlastus- või otsustusetappi.</p>
    <p>Kasutaja saab süsteemi laadida eelnõu või väljatöötamiskavatsuse faili, kirjeldada kavatsust vabas keeles, uurida õigusontoloogia graafi, küsida AI-lt kontrollitavaid küsimusi ning koostada uue eelnõu mustandit sammhaaval. Süsteem seob kasutaja töö olemasoleva õigusraamistikuga: kehtivad seadused, varasemad eelnõud, Riigikohtu lahendid, EL õigusaktid ja EL kohtulahendid.</p>
    <div class="note"><strong>Lühidalt:</strong> Seadusloome ei ole lihtsalt dokumendihaldus ega juturobot. Selle keskmes on õigusontoloogia: masinloetav seoste võrk, mille kaudu saab küsida, millist normi, mõistet, kohtupraktikat või EL kohustust kavandatav muudatus puudutab.</div>

    <h3>Mida saab teha?</h3>
    <div class="grid-2">
      <div class="card"><strong>Leida seoseid.</strong><br>Otsi sätet, seadust, eelnõud või kohtulahendit ja vaata selle naabreid graafis.</div>
      <div class="card"><strong>Analüüsida eelnõu.</strong><br>Laadi üles .docx või .pdf ja saa olemiotsing, mõjuanalüüs ning eksporditav aruanne.</div>
      <div class="card"><strong>Küsida nõu.</strong><br>AI vestlus kasutab ontoloogiat ja RAG-i, et vastused oleksid seotud allikatega.</div>
      <div class="card"><strong>Koostada mustand.</strong><br>AI koostaja juhib kavatsusest läbi küsimuste, uurimise, struktuuri, sätete ja ekspordini.</div>
    </div>
  </section>

  <section class="page-break">
    <h2>2. Miks seda vaja on?</h2>
    <p>Eesti õigusloome kvaliteet sõltub sellest, kui vara on näha kavandatava muudatuse tegelik mõju. Praktikas on õigusruum suur ja killustunud: ühe uue kohustuse lisamine võib puudutada andmekaitset, avalikku teavet, haldusmenetlust, karistusõigust, eriseadusi, Riigikohtu praktikat ja EL õigusakte korraga.</p>
    <p>Seadusloome aitab vähendada kolme tüüpilist riski.</p>
    <ul>
      <li><strong>Varjatud konfliktid:</strong> kavandatav norm võib korrata, kitsendada või vaidlustada kehtivat sätet.</li>
      <li><strong>Lüngad:</strong> eelnõu võib jätta reguleerimata rakendamise, järelevalve, sanktsioonid, üleminekusätted või EL direktiivi osad.</li>
      <li><strong>Teadmuse kadu:</strong> varasemad eelnõud ja kohtulahendid on olemas, kuid neid ei leita õigel hetkel üles.</li>
    </ul>

    <figure>
      <img class="diagram" src="assets/diagram_dataflow.svg" alt="Seadusloome andmevoo skeem">
      <figcaption>Skeem 1. Õigusallikad sünkroonitakse ontoloogiasse; kasutaja eelnõu analüüsitakse selle võrgustiku vastu.</figcaption>
    </figure>

    <p>Süsteemi andmekihis kasutatakse õigusallikate korpust, mis hõlmab muu hulgas 615 kehtivat seadust, 22 832 eelnõud, 12 137 Riigikohtu lahendit, 33 242 EL õigusakti ja 22 290 EL kohtulahendit. Kasutaja ei pea kõiki neid allikaid ise läbi otsima; ta saab alustada oma eelnõust, küsimusest või õiguslikust mõistest.</p>
  </section>

  <section class="page-break">
    <h2>3. Põhimõte ja töövoog</h2>
    <p>Seadusloome töövoog on iteratiivne. Ametnik sisestab kavatsuse või dokumendi, süsteem teeb automaatse analüüsi, kasutaja vaatab tulemused üle, parandab teksti ning kordab analüüsi kuni riskid ja ebaselgused on käsitletud.</p>
    <figure>
      <img class="diagram" src="assets/diagram_workflow.svg" alt="Tüüpiline kasutaja töövoog">
      <figcaption>Skeem 2. Tavapärane ring: sisend → analüüs → tõlgendus → otsus.</figcaption>
    </figure>
    <h3>Süsteemi põhiosad</h3>
    <table>
      <thead><tr><th>Osa</th><th>Kasutaja vaates</th><th>Milleks kasutada</th></tr></thead>
      <tbody>
        <tr><td><span class="pill">Töölaud</span></td><td>Ülevaade teie eelnõudest, vestlustest, koostamissessioonidest ja järjehoidjatest.</td><td>Alustamiseks, pooleliolevate tööde leidmiseks ja viimaste tegevuste kontrolliks.</td></tr>
        <tr><td><span class="pill">Uurija</span></td><td>D3 graaf, otsing, kategooriad, detailpaneel ja ajafilter.</td><td>Õigusaktide, sätete, mõistete, kohtupraktika ja EL seoste uurimiseks.</td></tr>
        <tr><td><span class="pill">Eelnõud</span></td><td>Faili üleslaadimine, töötluse staatus, mõjuaruanne ja kustutamine.</td><td>Olemasoleva eelnõu või VTK automaatseks analüüsiks.</td></tr>
        <tr><td><span class="pill">Koostaja</span></td><td>Seitsmesammuline AI töövoog kavatsusest DOCX mustandini.</td><td>Uue seaduse või VTK eeltöö loomiseks.</td></tr>
        <tr><td><span class="pill">Vestlus</span></td><td>Allikatega põhjendatud AI nõustaja, vajadusel seotud konkreetse eelnõuga.</td><td>Küsimuste, võrdluste ja kontrollnimekirjade jaoks.</td></tr>
      </tbody>
    </table>
  </section>

  <section class="page-break">
    <h2>4. Töölaud ja navigeerimine</h2>
    <p>Töölaud on kasutaja alguspunkt. Siit näeb pooleliolevaid eelnõusid, koostamissessioone, vestlusi, organisatsiooni infot, järjehoidjaid ja viimaseid tegevusi. Vasak menüü viib põhivaadete juurde.</p>
    <figure>
      <img class="screen" src="assets/screenshot_dashboard.png" alt="Töölaua ekraanivaate näide">
      <figcaption>Ekraanivaate näide. Töölaud koondab kiirlingid, tegevused ja järjehoidjad.</figcaption>
    </figure>
    <h3>Hea alustamisjärjekord</h3>
    <ol>
      <li>Kui teil on olemas eelnõu või VTK fail, avage <strong>Eelnõud</strong> ja laadige dokument üles.</li>
      <li>Kui teil on vaid eesmärk või probleemikirjeldus, avage <strong>Koostaja</strong> ja alustage kavatsuse kirjeldamisest.</li>
      <li>Kui soovite enne kirjutamist õigust uurida, avage <strong>Uurija</strong> ja otsige lähim seadus, säte või mõiste.</li>
      <li>Kui teil on konkreetne küsimus, avage <strong>Vestlus</strong>; eelnõu detailvaatest saab vestluse siduda dokumendi kontekstiga.</li>
    </ol>
  </section>

  <section class="page-break">
    <h2>5. Eelnõud ja mõjuanalüüs</h2>
    <p><strong>Eelnõud</strong> on koht, kus üles laadida .docx või .pdf dokumente ja jälgida automaatset töötlust. Sama vaade toetab tavalist eelnõud ja VTK-d. Kui eelnõu põhineb VTK-l, saab need omavahel siduda, et hilisem analüüs näitaks ka arenguloogikat.</p>
    <figure>
      <img class="screen" src="assets/screenshot_drafts.png" alt="Eelnõude nimekirja ekraanivaate näide">
      <figcaption>Ekraanivaate näide. Eelnõude tabelis saab filtreerida tüübi, staatuse, üleslaadija ja kuupäeva järgi.</figcaption>
    </figure>
    <h3>Mis juhtub pärast üleslaadimist?</h3>
    <ol>
      <li><strong>Üles laaditud:</strong> fail salvestatakse krüpteeritult ja lisatakse taustatöö järjekorda.</li>
      <li><strong>Töötlemine:</strong> tekst eraldatakse dokumendist Apache Tika abil.</li>
      <li><strong>Olemite eraldamine:</strong> süsteem leiab seaduste, paragrahvide, EL aktide, kohtulahendite ja õigusmõistete viited.</li>
      <li><strong>Mõjude analüüs:</strong> SPARQL-päringud ja AI analüüs võrdlevad eelnõud ontoloogiaga.</li>
      <li><strong>Valmis:</strong> detailvaates saab avada mõjuaruande, minna graafi mõjutatud üksusi vaatama või eksportida aruande DOCX-formaadis.</li>
    </ol>
    <div class="note warning"><strong>Näide:</strong> kui laadite üles eelnõu „Tehisintellekti kasutamine avalikus sektoris“, võib süsteem leida seosed isikuandmete kaitse, avaliku teabe, haldusmenetluse, järelevalve pädevuse ja EL AI määrusega. Aruanne aitab näha, kas tekstis on olemas õiguslik alus, vastutav asutus, vaidlustamise kord ja üleminekusätted.</div>
  </section>

  <section class="page-break">
    <h2>6. Ontoloogia uurija</h2>
    <p><strong>Uurija</strong> on interaktiivne graafivaade. See sobib olukorraks, kus tahate ise liikuda õigusruumi seoste vahel: alustada seadusest, sätetest, eelnõust, Riigikohtu lahendist või EL õigusaktist ning uurida, mis on nendega seotud.</p>
    <figure>
      <img class="screen" src="assets/screenshot_explorer.png" alt="Ontoloogia uurija ekraanivaate näide">
      <figcaption>Ekraanivaate näide. Graafis saab otsida, suumida, avada detailpaneeli ning lisada olulisi üksusi järjehoidjatesse.</figcaption>
    </figure>
    <h3>Mida uurijas teha?</h3>
    <ul>
      <li><strong>Otsida:</strong> sisestage seaduse nimi, paragrahv, mõiste, eelnõu või kohtulahendi tunnus.</li>
      <li><strong>Drill-down:</strong> avage esmalt kategooria, seejärel konkreetne olem ja selle naabrid.</li>
      <li><strong>Vaadata detaili:</strong> detailpaneel näitab metaandmeid, seoseid, versiooniajalugu ja allika linki.</li>
      <li><strong>Seostada eelnõuga:</strong> eelnõu detailist avatud graaf saab esile tõsta selle eelnõu mõjutatud üksused.</li>
      <li><strong>Järjehoidja:</strong> salvestage sageli kasutatavad sätted või lahendid töölauale.</li>
    </ul>
    <p class="footer-note">Praktiline nipp: alustage laia kategooriaga ja piirake vaadet järk-järgult. Suurte õigusvõrgustike puhul ei renderda süsteem korraga kümneid tuhandeid sõlmi, vaid laeb andmeid vajaduse järgi.</p>
  </section>

  <section class="page-break">
    <h2>7. AI vestlus</h2>
    <p><strong>Vestlus</strong> on õigusnõustaja, mis kasutab ontoloogiat, RAG-otsingut ja vajadusel tööriistapäringuid. Vestluse saab jätta üldiseks või siduda konkreetse eelnõuga, et AI arvestaks selle dokumendi mõjuaruannet ja konteksti.</p>
    <figure>
      <img class="screen" src="assets/screenshot_chat.png" alt="AI vestluse ekraanivaate näide">
      <figcaption>Ekraanivaate näide. Vestlus vastab allikapõhiselt ja kuvab kasutuse limiidi olekut.</figcaption>
    </figure>
    <h3>Head küsimuse näited</h3>
    <ul>
      <li>„Milliseid kehtivaid sätteid puudutab eelnõu, kui reguleerime AI kasutamist avalikus sektoris?“</li>
      <li>„Võrdle seda kavatsust isikuandmete kaitse üldmääruse artikli 6 nõuetega.“</li>
      <li>„Leia Riigikohtu lahendid, mis käsitlevad avaliku teabe juurdepääsupiiranguid.“</li>
      <li>„Koosta kontrollnimekiri, mida peaksin enne kooskõlastusringi üle vaatama.“</li>
      <li>„Selgita, millised sätted võivad vajada rakendusakte või üleminekusätteid.“</li>
    </ul>
    <div class="note"><strong>Oluline:</strong> AI vastust tuleb käsitleda nõuandva töövahendina. Kontrollige viidatud allikaid ja otsustage lõplik õiguslik lahendus ametliku menetluse, juristi ja poliitikakujundaja rollide järgi.</div>
  </section>

  <section class="page-break">
    <h2>8. AI koostaja</h2>
    <p><strong>Koostaja</strong> on mõeldud uue õigusakti või VTK eeltöö jaoks, kui alguses on olemas probleem, eesmärk või poliitikavalik, kuid mitte veel terviklik tekst. Süsteem juhib töö läbi seitsme sammu.</p>
    <figure>
      <img class="diagram" src="assets/diagram_drafter_steps.svg" alt="AI koostaja seitsme sammu skeem">
      <figcaption>Skeem 3. AI koostaja töövoog kavatsusest ekspordini.</figcaption>
    </figure>
    <figure>
      <img class="screen" src="assets/screenshot_drafter.png" alt="AI koostaja ekraanivaate näide">
      <figcaption>Ekraanivaate näide. Esimeses sammus kirjeldab kasutaja kavatsuse vabas vormis.</figcaption>
    </figure>
    <h3>Näide: AI avalikus sektoris</h3>
    <p>Kasutaja kirjeldab kavatsust: „Soovin reguleerida tehisintellekti kasutamist avalikus sektoris, sh andmekaitset, läbipaistvust ja inimjärelevalvet.“ Koostaja küsib täpsustusi: millised asutused on hõlmatud, kas seadus loob uue järelevalvepädevuse, millised riskitasemed kuuluvad keelatud või kõrge riskiga kasutuse alla ning kuidas lahendatakse registri- ja aruandluskohustus.</p>
    <p>Pärast täpsustusi uurib süsteem ontoloogiat, pakub struktuuri, koostab sätted, käivitab integreeritud mõjuülevaate ja võimaldab eksportida DOCX mustandi. Kasutaja saab igas etapis teksti muuta; AI ei lukusta otsust.</p>
  </section>

  <section class="page-break">
    <h2>9. Kasutuslood ja näited</h2>
    <table>
      <thead><tr><th>Kasutuslugu</th><th>Kuidas alustada</th><th>Tulemus</th></tr></thead>
      <tbody>
        <tr><td>Uue seaduse kavatsus</td><td>Koostaja → „Alusta uut koostamist“ → kirjelda eesmärk ja sihtrühm.</td><td>Täpsustatud küsimused, struktuur, sätted ja DOCX mustand.</td></tr>
        <tr><td>Olemasoleva eelnõu kontroll</td><td>Eelnõud → laadi .docx/.pdf üles → oota staatust „Valmis“.</td><td>Mõjuaruanne, viidatud allikad, konfliktid ja lüngad.</td></tr>
        <tr><td>EL direktiivi ülevõtmine</td><td>Vestlus või Uurija → otsi direktiiv või seotud EL õigusakt.</td><td>Nimekiri Eesti sätetest ja võimalikest katmata nõuetest.</td></tr>
        <tr><td>Kohtupraktika kontroll</td><td>Uurija → otsi säte või mõiste → vaata seotud Riigikohtu lahendeid.</td><td>Asjakohased lahendid enne normi sõnastamist.</td></tr>
        <tr><td>Kooskõlastusringi ettevalmistus</td><td>Vestlus → palu kontrollnimekirja või riskiülevaadet eelnõu kohta.</td><td>Kontrollitav nimekiri lahtistest küsimustest.</td></tr>
        <tr><td>Organisatsiooni teadmuse hoidmine</td><td>Kasuta järjehoidjaid, kinnitatud vestlusi ja eelnõu seoseid.</td><td>Hiljem leitav põhjendus ja allikate rada.</td></tr>
      </tbody>
    </table>

    <h2>10. Turvalisus ja head tavad</h2>
    <p>Eelnõud ja VTK-d võivad enne avalikustamist olla poliitiliselt tundlikud. Seetõttu kasutab Seadusloome organisatsioonipõhist ligipääsu, krüpteeritud failisalvestust, auditeerimist ja kontrollitud kustutamist.</p>
    <ul>
      <li><strong>Organisatsiooni piir:</strong> kasutaja näeb ainult oma organisatsiooni eelnõusid, vestlusi ja aruandeid.</li>
      <li><strong>Krüpteerimine:</strong> üleslaetud failid ja parsitud tekst salvestatakse krüpteeritult.</li>
      <li><strong>Audit:</strong> olulised tegevused, nagu üleslaadimine, vaatamine, kustutamine ja vestlusmuudatused, logitakse.</li>
      <li><strong>Säilitamine:</strong> eelnõu säilib kuni omanik selle kustutab; 90 päeva tegevusetuse korral kuvatakse alleshoidmise hoiatus.</li>
      <li><strong>Kustutamine:</strong> kustutamisel eemaldatakse fail, seotud nimega graaf ja andmebaasikirjed.</li>
    </ul>
    <div class="note warning"><strong>Hea tava:</strong> ärge sisestage vestlusse rohkem isikuandmeid või salastatud detaile, kui analüüsiks vajalik. Hoidke poliitilised valikud ja lõplik õiguslik otsus väljaspool AI automaatset soovitust ning dokumenteerige, miks otsus tehti.</div>
    <h3>Kiirkontroll enne töö lõpetamist</h3>
    <ul>
      <li>Kas mõjuaruanne on valmis ja kriitilised seosed läbi vaadatud?</li>
      <li>Kas EL, Riigikohtu ja kehtivate sätete viited on kontrollitud algallikast?</li>
      <li>Kas vestluses saadud soovitused on vajadusel eelnõu tekstis või seletuskirjas põhjendatud?</li>
      <li>Kas tundlik mustand on vajadusel kustutatud või teadlikult alles hoitud?</li>
    </ul>
  </section>
</body>
</html>"""
    HTML_PATH.write_text(html, encoding="utf-8")


def find_chrome() -> Path:
    for path in CHROME_PATHS:
        if path.exists():
            return path
    raise RuntimeError("Headless Chrome was not found.")


def print_pdf() -> None:
    chrome = find_chrome()
    cmd = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--allow-file-access-from-files",
        f"--print-to-pdf={PDF_PATH}",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        HTML_PATH.as_uri(),
    ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    screenshot_dashboard()
    screenshot_explorer()
    screenshot_drafts()
    screenshot_chat()
    screenshot_drafter()
    write_svg_assets()
    write_manual_html()
    print_pdf()
    print(f"Wrote {PDF_PATH}")


if __name__ == "__main__":
    main()
