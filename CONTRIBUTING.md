# Contributing

Thanks for helping. A few rules keep this public repository tidy.

## Commits

- Every commit on `main` is authored by the maintainer with a GitHub noreply address:
  `kirvis13 <7209653+kirvis13@users.noreply.github.com>` (or `kirvis13@users.noreply.github.com`,
  which the GitHub website uses). Commits made on the website are committed by `GitHub`.
- Pull requests are squash-merged, so your commits in a pull request may carry your own name.
- No co-author or session trailers in commit messages (`Co-Authored-By:`, `...-Session:`), and no
  "generated with" lines in pull requests, release notes or issues.
- Never a real e-mail address in a commit.
- The CI job `authorship` in `.github/workflows/validate.yml` checks this on every push to `main`.

## Versions and releases

- `custom_components/weheat_monitor/manifest.json` holds the version (Semantic Versioning).
- Every change a user notices: bump the version and add an entry at the top of `CHANGELOG.md`, in
  the same commit.
- A release is a tag `vX.Y.Z` equal to the manifest version, plus a GitHub release with the
  CHANGELOG entry as its text. The maintainer creates releases; CI checks that the tag matches
  the manifest.

## Checks

```bash
pip install -r requirements_test.txt
pytest -q
```

`logic.py`, `engine.py`, `api.py`, `units.py` and `const.py` stay free of Home Assistant imports, so
the tests run without Home Assistant. CI also runs `hassfest` and the HACS action.

## The API

The integration talks to one endpoint, `POST /api/v1/devices/sync` on weheat-monitor.nl, with the
key as `Authorization: Bearer whm_...`. Field names and limits are described in the README and in
`tests/fake_api.py`, which mimics the server.
