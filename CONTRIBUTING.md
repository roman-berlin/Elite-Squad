# Contributing to SQUAD

Thanks for helping build SQUAD — the autonomous dev-team orchestrator. Contributions of
all sizes are welcome: bug reports, docs fixes, test harnesses, and features.

## Dev setup (5 minutes)

```bash
git clone https://github.com/roman-berlin/Elite-Squad.git
cd Elite-Squad
python3 -m venv .venv && source .venv/bin/activate   # needs Python 3.12
pip install -r requirements.txt
python3 tests/run_all.py
```

The test suite must be green before and after your change. Every harness stubs the
Claude Agent SDK — **no network, no real model calls, and no API key** are needed to
develop or run the tests.

## Ground rules

- **Python 3.12 only.** Dependencies live in `requirements.txt`; open an issue to discuss
  before adding a new one. No Node/Bun tooling.
- **PRs target `dev`**, never `main`. `main` is the release branch; the maintainer merges
  `dev → main` after QA.
- **Tests are the gate.** Every behaviour change ships a `tests/<name>_test.py` harness
  in the same PR. Run `python3 tests/run_all.py` and paste the tail of its output in the PR.
- **Stay in scope.** One issue per PR; file separate issues for things you find along the way.

## Finding something to work on

- Issues labelled [`good first issue`](https://github.com/roman-berlin/Elite-Squad/labels/good%20first%20issue)
- [`Documentation/REVIEW_BACKLOG.md`](Documentation/REVIEW_BACKLOG.md) — the deferred tech-debt queue
- [`ROADMAP.md`](ROADMAP.md) — where the project is heading

If you want to take an issue, comment on it first so work isn't duplicated.

## Sign-off and licensing

By submitting a contribution you certify the
[Developer Certificate of Origin](https://developercertificate.org/) — sign your commits
with `git commit -s` (adds `Signed-off-by:`).

Contributions are accepted under the project license (AGPL-3.0-only). You additionally
grant the project maintainer the right to include your contribution in a future
commercially licensed (dual-licensed) edition of SQUAD. If that grant is a problem for
you, say so in the PR and we'll discuss options.

## Conduct

Be kind. The full policy is in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
