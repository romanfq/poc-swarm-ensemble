#!/usr/bin/env bash
# Render docs/SPEC.md to a PDF. Run from anywhere; paths are resolved relative
# to docs/ so that the figure references in SPEC.md ("figures/figN-*.pdf")
# resolve as they do on GitHub.
#
#   docs/build/build.sh                  -> docs/build/DAGS-spec-v2.0.pdf
#   docs/build/build.sh /tmp/spec.pdf    -> that path
#
# Needs: pandoc (>= 3.1) and xelatex. See README.md in this directory.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.."                     # docs/

OUT=${1:-build/DAGS-spec-v2.0.pdf}

command -v pandoc  >/dev/null || { echo "pandoc not found"  >&2; exit 1; }
command -v xelatex >/dev/null || { echo "xelatex not found" >&2; exit 1; }

pandoc SPEC.md \
  --from=markdown+raw_tex+pipe_tables+yaml_metadata_block \
  --pdf-engine=xelatex \
  --template=build/template.tex \
  --include-in-header=build/preamble.tex \
  -V mainfont="Archivo-Regular.ttf" \
  -V mainfontoptions="Path=build/fonts/,BoldFont=Archivo-Bold.ttf,ItalicFont=Archivo-Italic.ttf,BoldItalicFont=Archivo-BoldItalic.ttf" \
  -V monofont="IBMPlexMono-Regular.ttf" \
  -V monofontoptions="Path=build/fonts/,BoldFont=IBMPlexMono-Bold.ttf,ItalicFont=IBMPlexMono-Italic.ttf,Scale=0.90" \
  --toc --toc-depth=2 \
  -V documentclass=article \
  -V papersize=a4 \
  -V geometry:"a4paper,textwidth=159mm,top=25mm,bottom=25mm" \
  -V linestretch=1.15 \
  -V fontsize=10pt \
  -o "$OUT"

pages=$(pdfinfo "$OUT" 2>/dev/null | awk '/^Pages/{print $2}')
echo "built $OUT ($(du -h "$OUT" | cut -f1)${pages:+, $pages pages})"
