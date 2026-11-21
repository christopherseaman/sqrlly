Read `brief.md` before editing. Use only the files already in this repository.
Use local file-editing tools to create the required files.

Generate the requested tiny browser app under `app/`.

Required files and contracts:

- `app/package.json` must set `type` to `module` and define `test` as
  `node --test test/app.test.js`.
- `app/app.js` must export a pure `formatQuote()` function and render the quote
  into the page when loaded in a browser. Keep browser code dependency-free.
- `app/index.html` must load `./app.js` as a module and contain a `<main>`.
- `app/test/app.test.js` must import `formatQuote` and test the two requested
  line items plus the `$198.00` total using Node's built-in `node:test` module.

Use only the values in `brief.md`. Do not access the network, run package
installation, add dependencies, or modify files outside `app/`. Local file
inspection and editing are allowed.
