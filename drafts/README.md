# Drafts

Issue drafts, filed with `swarm.py backend file drafts/NAME.md` (dry run) and
`--apply`. A filed draft moves to `filed/` with its issue URL in the frontmatter.

```markdown
---
labels: [next-version]        # plain labels that already exist on the tracker
autonomy: human-must-review   # sets swarm:autonomy:
repo: OWNER/app               # must be under repos: in backend.yaml; sets repo:
epic: 7                       # required
depends_on: [78]
status: blocked               # optional; `ready` is refused
---
# The title is the H1

The body is everything below it, verbatim.
```
