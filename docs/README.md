# User guide

`User-Guide.docx` and `User-Guide.pdf` are a printable two-page guide to the tool.

They are generated, not hand-edited. The content is in `user-guide.json`; the
layout, colours and logo come from Kokka & Company's design system
(`@kokka-company/design`), so they stay on-brand by construction. To regenerate
(maintainers with access to the design package only):

```bash
kokka new general --data docs/user-guide.json -o docs/User-Guide.docx
kokka validate docs/User-Guide.docx
soffice --headless --convert-to pdf --outdir docs docs/User-Guide.docx
```

The `general` template prints a placeholder "Column" header row above the
table. The first row of `table` in `user-guide.json` (Problem / Cause / Fix) is
the real header: after regenerating, move it into the header row of the
`.docx` before converting to PDF.

The Kokka & Company logo and letterhead in these files are trademarks and are
not covered by the GNU GPL. See [../TRADEMARKS.md](../TRADEMARKS.md).
