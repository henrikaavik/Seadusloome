# Aino Typeface License

**Status:** Pending confirmation from brand@estonia.ee

## Summary

The Seadusloome application uses the Aino typeface as its primary font, sourced
from the official Estonia Brand design system at https://brand.estonia.ee/.
Aino was designed by Anton Koovit and is distributed by Brand Estonia.

## Intended use

- Government advisory tool for Estonian ministry officials
- Internal, non-commercial government use
- Falls within the scope of "introducing the state in ways that are consistent
  and uniform" — the stated purpose of Brand Estonia

## Action required

1. Email brand@estonia.ee to confirm that Aino may be self-hosted as WOFF2
   files on the production Coolify-managed server (seadusloome.sixtyfour.ee).
2. Record the response verbatim in this document.
3. If the license does not permit this use, switch the primary typeface to
   Inter (open-source, SIL Open Font License) and update `fonts.css` and
   `tokens.py` accordingly.

## Current status

- Aino files are **not** committed to the repository (see `.gitignore`).
- `fonts.css` loads them from `/static/fonts/aino/*.woff2` with a Verdana
  fallback.
- Until Aino is confirmed and downloaded, the production site renders in
  Verdana.

## License confirmation (to be filled in)

```
Date sent:
Sender email:
Response received:
Response date:
Response text:
Conclusion:
```
