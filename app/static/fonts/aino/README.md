# Aino Font Files

Download the Aino typeface WOFF2 files from
https://brand.estonia.ee/guidelines/typography/ and place them here:

- Aino-Regular.woff2
- Aino-Bold.woff2
- Aino-BoldItalic.woff2
- Aino-Headline.woff2

The application falls back to Verdana when these files are missing.

Until WOFF2 files are placed here, the @font-face rules in
`app/static/css/fonts.css` are commented out to prevent 404s. After
dropping the files into this directory, uncomment the four
@font-face blocks in that file to enable Aino.

See `docs/legal/aino-license.md` for licensing status.
