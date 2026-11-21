import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const required = [
  'app/package.json',
  'app/index.html',
  'app/app.js',
  'app/test/app.test.js',
];
const missing = required.filter((path) => {
  try { readFileSync(path); return false; } catch { return true; }
});
const html = missing.length ? '' : readFileSync('app/index.html', 'utf8');
const shapeOk = html.includes('<main') && html.includes('type="module"') && html.includes('./app.js');
const test = spawnSync('node', ['--test', 'app/test/app.test.js'], { encoding: 'utf8' });
let quoteShapeOk = false;
if (!missing.length) {
  const { formatQuote } = await import(pathToFileURL(resolve('app/app.js')));
  if (typeof formatQuote === 'function') {
    const quote = formatQuote();
    quoteShapeOk = typeof quote === 'string'
      && ['Northwind Bikes', 'Setup', 'Monthly support', '$198.00'].every((part) => quote.includes(part));
  }
}
const result = {
  required_files: required.length - missing.length,
  missing,
  browser_shape: shapeOk,
  quote_shape: quoteShapeOk,
  test_exit_code: test.status,
  passed: missing.length === 0 && shapeOk && quoteShapeOk && test.status === 0,
};
writeFileSync('app/acceptance.json', `${JSON.stringify(result, null, 2)}\n`);
console.log(JSON.stringify(result));
if (!result.passed) process.exit(1);
