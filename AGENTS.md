# Guidelines for AI assistants

lazyzephyr is a public open-source project. These rules apply to every AI
assistant working in this repository.

## Keep private details out

- Never put company names, product names, internal ticket ids, internal repo
  names or local workspace paths anywhere: not in code, tests, docs, examples,
  commit messages or PR descriptions.
- Example values are where leaks usually slip in: board, platform, fixture and
  device names in tests, docs and config examples. Use upstream Zephyr names
  only, such as `native_sim`, `qemu_x86`, `frdm_k64f` or `gpio_loopback`.
- When you borrow conventions from another repository, copy the general rule,
  never its names, ticket formats or examples.
- Before committing or pushing, search the whole history, not just the latest
  commit:

  ```sh
  git log -p --all | grep -i -E '<names to check>'
  ```

- If a leak has already been pushed, rewrite the affected commits and
  force-push. Ask first, because force-pushing rewrites public history.

## Commits

- Subject: `type(area): Subject`, at most 72 characters, with a capitalised
  subject.
  - `type` is `feat`, `fix`, `docs`, `test`, `ci`, `refactor`, `chore`, and so
    on.
  - `area` is the tab or part touched: `build`, `dts`, `kconfig`, `tests`,
    `docs` and so on. Leave it out when a change spans the whole app.
- Body: short and to the point, wrapped at 72 characters. Say why, not what
  the diff already shows.
- End every commit with these trailers:

  ```
  Assisted-by: <tool>:<exact model id>
  Signed-off-by: <maintainer name> <maintainer git user.email>
  ```

- Never add `Co-Authored-By`, session links or other AI trailers.
- The author is the maintainer from `git config`, not an AI identity. Check
  with `git log --format='%an <%ae>'` after rewriting history.

## Workflow

- Don't commit to `main`. Work on a branch and open a pull request. PRs are
  rebase-merged, so each commit lands on `main` as it is and must stand on its
  own.
- Ask before committing, pushing or force-pushing, unless the maintainer
  has already said to go ahead.
- Keep a branch's history clean. Fold follow-up fixes into the commit they
  belong to instead of stacking "fix review" commits. While rewriting, keep a
  backup branch, and check that the final code matches it
  (`git diff backup HEAD`) before deleting it.
- Releases are made by release-please. Don't edit `CHANGELOG.md` or the
  version by hand.

## Development

- Run the tests in the Nix dev shell. The system Python lacks the
  dependencies:

  ```sh
  nix develop -c python -m pytest -q
  ```

- To try a branch as the installed `lazyzephyr`:

  ```sh
  nix profile install "git+file://$PWD?ref=<branch>"   # or: nix profile upgrade lazyzephyr
  ```

  Nix only sees committed code, so commit before installing.
- When a change affects what users see, update the matching page in `docs/`
  (`build.md`, `devicetree.md`, `kconfig.md`, `tests.md`) and the in-app help.
- UI changes get a Textual pilot test next to the existing ones in `tests/`.
