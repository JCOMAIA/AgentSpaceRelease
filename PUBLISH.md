# Publishing this by hand

Everything here was built from the tag `v0.1.0`, which exists locally and has
not been pushed anywhere. Nothing in this folder touches the network.

## What is in the box

| | |
|---|---|
| `source/` | the 101 tracked files, exactly as the tag has them — unpacked, in case you want to read or drag them |
| `agentspace-0.1.0.tar.gz` | the same tree, for the Release page |
| `agentspace-0.1.0.zip` | the same tree, for people on Windows |
| `CHECKSUMS.txt` | sha256 of both archives |
| `RELEASE_NOTES.md` | paste this into the Release description |

The archives were built with `git archive` from the tag, so they contain
exactly what is tracked and nothing else: no `.env`, no `data/`, no `.venv`,
no editor state. Both were checked for the VPS address, its password, your
email and your domain. None appear, in the files or in the 30 commits of
history.

## 1. Create the repository

On github.com: **New repository**, name it, **do not** tick "Add a README",
"Add .gitignore" or "Choose a licence" — the tree already has all three, and
an initialised repo means resolving a merge before the first push.

## 2. Push

```bash
git remote add origin https://github.com/<you>/agentspace.git
git push -u origin master
git push origin v0.1.0
```

GitHub makes the first branch you push the default one, so this gives you a
repo whose default branch is `master`. If you would rather it be `main`,
rename before pushing — the tag is unaffected:

```bash
git branch -m master main
git push -u origin main
git push origin v0.1.0
```

## 3. Cut the Release

**Releases** → **Draft a new release**.

- **Tag**: `v0.1.0` — it is already pushed, so pick it from the list rather
  than creating one.
- **Title**: `AgentSpace 0.1.0`
- **Description**: the contents of `RELEASE_NOTES.md`.
- **Attach**: `agentspace-0.1.0.tar.gz`, `agentspace-0.1.0.zip` and
  `CHECKSUMS.txt`.

GitHub attaches its own auto-generated `Source code (zip)` and `(tar.gz)` as
well. Those are the same bytes; the ones here exist so the checksums mean
something.

### Or from the command line

```bash
gh release create v0.1.0 \
  --title "AgentSpace 0.1.0" \
  --notes-file release/RELEASE_NOTES.md \
  release/agentspace-0.1.0.tar.gz \
  release/agentspace-0.1.0.zip \
  release/CHECKSUMS.txt
```

## 4. After the repo exists

The relative links in the release notes (`ForLLMInstall.md`,
`docs/DEPLOY_VPS.md`, `LICENSE`) only resolve once the files are on GitHub, so
push before you publish the release, not after.

Two things worth setting while you are there:

- **About** → description and the link to the running instance.
- **Settings → Security → Private vulnerability reporting**, since
  `SECURITY.md` asks people not to open public issues for holes.

## Rebuilding this folder

It is derived, not source, and `.gitignore` keeps it out of the repo:

```bash
rm -rf release && mkdir release
git archive --format=tar --prefix=agentspace-0.1.0/ v0.1.0 | (cd release && tar -xf -)
mv release/agentspace-0.1.0 release/source
git archive --format=zip    --prefix=agentspace-0.1.0/ -o release/agentspace-0.1.0.zip    v0.1.0
git archive --format=tar.gz --prefix=agentspace-0.1.0/ -o release/agentspace-0.1.0.tar.gz v0.1.0
(cd release && sha256sum agentspace-0.1.0.tar.gz agentspace-0.1.0.zip > CHECKSUMS.txt)
```

## What was checked before this was built

- 377 tests pass in the working tree.
- The tarball was extracted into an empty directory, installed into a fresh
  virtualenv, and its suite run from there: 377 passed, 12 skipped.
- That extracted copy was started with `SANDBOX_DRIVER=none`. It applied its
  own migrations, served `/api/v1/hello` advertising eight capabilities and no
  execution, and rendered `/pricing`.
- Every command quoted in `RELEASE_NOTES.md` was run against the CLI it
  describes. One was wrong on the first pass and was corrected.

## If you want to undo the tag

```bash
git tag -d v0.1.0
```

It is local only until you push it.
