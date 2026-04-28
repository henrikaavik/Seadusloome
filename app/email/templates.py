"""Estonian transactional email templates returning ``(subject, html, text)``."""

from __future__ import annotations


def password_reset(*, full_name: str, reset_url: str) -> tuple[str, str, str]:
    subject = "Parooli lähtestamine — Seadusloome"
    html = f"""\
<p>Tere {full_name},</p>

<p>Saime taotluse teie parooli lähtestamiseks Seadusloome platvormil.
Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{reset_url}">{reset_url}</a></p>

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
    return subject, html, text


def password_reset_admin(
    *, full_name: str, reset_url: str, admin_name: str
) -> tuple[str, str, str]:
    subject = "Administraator on lähtestanud teie parooli — Seadusloome"
    html = f"""\
<p>Tere {full_name},</p>

<p>Administraator <strong>{admin_name}</strong> on algatanud teie parooli lähtestamise
Seadusloome platvormil. Uue parooli määramiseks klõpsake allpool oleval lingil:</p>

<p><a href="{reset_url}">{reset_url}</a></p>

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
    return subject, html, text
