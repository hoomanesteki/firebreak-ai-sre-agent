# Prompts

SPEC.md Section 10.4: prompts for the commander, the specialists, the critic and the
reporter are versioned files with ids and hashes.

**Why they are files rather than string literals.** Three reasons, and the third is
the one that matters.

1. A prompt is configuration, and an operator changing one should not edit Python.
2. `firebreak optimize` writes a candidate prompt, and a candidate has to be diffable
   against the one it replaces in a pull request.
3. **A prompt is an input to every measurement this project produces.** A report
   quoting top-1 accuracy is quoting it for a particular set of prompts, and if those
   prompts are literals scattered through the code then no number can be reproduced
   later. The hash is what ties a report to the prompts that produced it.

**The hash covers the body, not the file.** Frontmatter carries the id, the version
and the node, and editing a comment must not change the hash, because a hash that
moved for a comment would make every report look stale after a typo fix. What the hash
covers is exactly the text the model sees.

**Optimization never reads validation or test tasks.** SPEC.md Section 10.4 and Phase
10's reviewer focus. `firebreak optimize` takes the train split and nothing else,
which is asserted in `tests/leakage/`, and a candidate must pass the eval gate on
validation through a normal pull request before it ships.

## Files

| id | node | what it is for |
|---|---|---|
| `commander/v1` | commander | Decide which specialists test which hypothesis next |
| `specialist/v1` | specialists | Answer one narrow question about one signal |
| `critic/v1` | critic | Try to refute the leading hypothesis |
| `reporter/v1` | reporter | Write the report as claims that cite evidence |

## Conventions

- One file per node per version: `<node>.v<n>.md`.
- Frontmatter: `id`, `node`, `version`, `optimized_from` when a candidate came out of
  `firebreak optimize`, and `notes`.
- The body is the prompt. Everything below the frontmatter, verbatim, is what the model
  sees.
- A new version is a new file. Editing a shipped prompt in place would silently change
  what every past report meant.
