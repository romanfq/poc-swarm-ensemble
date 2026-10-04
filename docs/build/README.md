# Building the spec PDF

`docs/SPEC.md` is the source of truth. This directory holds everything needed to
render it, so the PDF is a build artifact and is **not** committed.

```bash
docs/build/build.sh            # -> docs/build/DAGS-spec-v2.0.pdf
```

## Prerequisites

| Tool | macOS |
|---|---|
| pandoc >= 3.1 | `brew install pandoc` |
| xelatex + `pdfinfo` | `brew install --cask mactex-no-gui` (or BasicTeX + `tlmgr install titlesec fancyhdr colortbl newunicodechar`) |

## What is in here

| File | Why |
|---|---|
| `build.sh` | The pandoc invocation. Resolves paths relative to `docs/`, so SPEC.md's `figures/figN-*.pdf` references work both here and when GitHub renders the markdown. |
| `preamble.tex` | The house style: the DAGS palette, navy headings via `titlesec`, the running footer, table rules, and a `newunicodechar` mapping for `✓` (Archivo has no such glyph). Also forces figures to `[H]` so they stay in document order. |
| `template.tex` | Pandoc 3.1's default LaTeX template with **one** change: line 112, `\usepackage{lmodern}`, is commented out. The stock template loads it unconditionally, it is not in a `mactex-no-gui` install, and `fontspec` supersedes it anyway. Without this patch the build fails with `lmodern.sty not found`. Re-patch the same line if you ever regenerate the template with `pandoc -D latex`. |
| `fonts/` | Archivo (text) and IBM Plex Mono (code), four weights each, both SIL Open Font Licence 1.1 — see `OFL-Archivo.txt` and `OFL-IBMPlexMono.txt`. Committed rather than fetched: the Archivo statics are instanced from the upstream variable font, and a download step would need network access the build should not assume. |

## Figures

`../figures/fig1..fig8-*.pdf` are vector PDFs, referenced from SPEC.md at
`{width=159mm}` — the text width, so they are reproduced 1:1.
