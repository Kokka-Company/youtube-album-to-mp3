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

The Kokka & Company logo and letterhead in these files are trademarks and are
not covered by the MIT License. See [../TRADEMARKS.md](../TRADEMARKS.md).
