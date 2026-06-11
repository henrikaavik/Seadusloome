"""Estonian transactional email templates returning ``(subject, html, text)``."""

from __future__ import annotations

import html


def password_reset(*, full_name: str, reset_url: str) -> tuple[str, str, str]:
    subject = "Parooli lähtestamine — Seadusloome"
    # Escape every user-controlled value before interpolating into the
    # HTML body. ``full_name`` originates from the user record and could
    # carry ``<``/``&``/``"`` that would otherwise inject markup; the
    # reset URL is escaped in both attribute and text position.
    name_html = html.escape(full_name)
    url_attr = html.escape(reset_url, quote=True)
    url_text = html.escape(reset_url)
    html_body = f"""\
<p>Tere {name_html},</p>

<p>Saime taotluse teie parooli lähtestamiseks Seadusloome platvormil.
Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{url_attr}">{url_text}</a></p>

<p>Link kehtib 1 tunni. Kui te ei taotlenud lähtestamist, võite selle e-kirja eirata —
teie parool jääb muutmata.</p>

<p>Lugupidamisega,<br>Seadusloome</p>
"""
    text = f"""\
Tere {full_name},

Saime taotluse teie parooli lähtestamiseks Seadusloome platvormil.
Uue parooli määramiseks avage:

{reset_url}

Link kehtib 1 tunni. Kui te ei taotlenud lähtestamist, võite selle e-kirja eirata —
teie parool jääb muutmata.

Lugupidamisega,
Seadusloome
"""
    return subject, html_body, text


def password_reset_admin(
    *, full_name: str, reset_url: str, admin_name: str
) -> tuple[str, str, str]:
    subject = "Administraator on lähtestanud teie parooli — Seadusloome"
    # Escape every user-controlled value (recipient name + the
    # admin-supplied name) before HTML interpolation; the reset URL is
    # escaped for both attribute and text position.
    name_html = html.escape(full_name)
    admin_html = html.escape(admin_name)
    url_attr = html.escape(reset_url, quote=True)
    url_text = html.escape(reset_url)
    html_body = f"""\
<p>Tere {name_html},</p>

<p>Administraator <strong>{admin_html}</strong> on algatanud teie parooli lähtestamise
Seadusloome platvormil. Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{url_attr}">{url_text}</a></p>

<p>Link kehtib 1 tunni.</p>

<p>Lugupidamisega,<br>Seadusloome</p>
"""
    text = f"""\
Tere {full_name},

Administraator {admin_name} on algatanud teie parooli lähtestamise
Seadusloome platvormil. Uue parooli määramiseks avage:

{reset_url}

Link kehtib 1 tunni.

Lugupidamisega,
Seadusloome
"""
    return subject, html_body, text
