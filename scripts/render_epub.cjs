/* Render the workflow's staged XHTML without changing the book or checkpoint. */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

function args(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i].startsWith('--') || argv[i + 1] === undefined) throw new Error('Expected --key value arguments');
    result[argv[i].slice(2)] = argv[i + 1];
  }
  if (!result.root || !result.out) throw new Error('Required: --root STAGING_DIRECTORY --out QA_DIRECTORY');
  return result;
}

function files(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const item = path.join(dir, entry.name);
    return entry.isDirectory() ? files(item) : [item];
  });
}

async function main() {
  const options = args(process.argv.slice(2));
  const root = path.resolve(options.root);
  const out = path.resolve(options.out);
  const modules = [options['module-dir'], path.resolve(__dirname, '..'), path.resolve(path.dirname(process.execPath), '..', 'node_modules'), process.cwd()].filter(Boolean);
  const { chromium } = require(require.resolve('playwright', { paths: modules }));
  if (options.browser && !fs.existsSync(options.browser)) throw new Error('The supplied browser executable does not exist');
  const candidates = [options.browser, 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe'].filter(Boolean);
  const executablePath = candidates.find(item => fs.existsSync(item));
  const documents = files(root).filter(item => /\.(?:xhtml|html)$/i.test(item) && /class=["'][^"']*\bunit\b/.test(fs.readFileSync(item, 'utf8'))).sort();
  if (!documents.length) throw new Error('No staged reading documents containing .unit found');
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ executablePath, headless: true, args: ['--disable-gpu', '--no-first-run', '--disable-extensions'] });
  const page = await browser.newPage({ viewport: { width: 600, height: 800 } });
  const checks = [];
  try {
    for (let d = 0; d < documents.length; d++) {
      for (const [width, fontSize] of [[360, 16], [600, 16], [360, 24]]) {
        await page.setViewportSize({ width, height: 800 });
        await page.goto(pathToFileURL(documents[d]).href);
        await page.addStyleTag({ content: `body { font-size: ${fontSize}px; }` });
        await page.evaluate(() => document.fonts.ready);
        const report = await page.evaluate(() => ({
          viewport: innerWidth,
          contentWidth: document.documentElement.scrollWidth,
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
          subtitles: document.querySelectorAll('h2,h3,h4,h5,h6').length,
          units: document.querySelectorAll('.unit').length,
          replacementCharacters: document.body.textContent.includes('\ufffd'),
          continuous: Array.from(document.querySelectorAll('.unit')).every(el => !['page', 'always'].includes(getComputedStyle(el).breakBefore)),
          xmlError: !!document.querySelector('parsererror'),
        }));
        checks.push({ document: path.relative(root, documents[d]), fontSize, ...report });
        if (d === 0 || d === documents.length - 1) await page.screenshot({ path: path.join(out, `chapter-${d + 1}-${width}-${fontSize}.png`) });
      }
    }
    await page.setViewportSize({ width: 600, height: 800 });
    await page.goto(pathToFileURL(documents[documents.length - 1]).href);
    await page.evaluate(() => document.querySelector('.unit:last-of-type')?.scrollIntoView());
    await page.screenshot({ path: path.join(out, 'last-unit.png') });
    const navFile = files(root).find(item => /^nav\.xhtml$/i.test(path.basename(item)));
    const navigation = { tested: false, pass: false, linksChecked: 0 };
    if (navFile) {
      await page.goto(pathToFileURL(navFile).href);
      const count = await page.locator('nav a[href]').count();
      navigation.tested = count > 0;
      navigation.pass = count > 0;
      for (const index of [...new Set([0, count - 1])].filter(i => i >= 0)) {
        await page.goto(pathToFileURL(navFile).href);
        await page.locator('nav a[href]').nth(index).click();
        navigation.pass = navigation.pass && await page.locator('.unit').count() > 0;
        navigation.linksChecked++;
      }
    }
    const totalUnits = checks.filter(c => c.viewport === 600 && c.fontSize === 16).reduce((sum, c) => sum + c.units, 0);
    const expected = options['expected-units'] === undefined ? totalUnits : Number(options['expected-units']);
    const failures = checks.filter(c => c.overflow || c.subtitles || c.replacementCharacters || !c.continuous || c.xmlError);
    const result = { status: failures.length === 0 && totalUnits === expected && navigation.pass ? 'PASS' : 'FAIL', totalUnits, expectedUnits: expected, documents: documents.length, checks, navigation, failures };
    fs.writeFileSync(path.join(out, 'render-report.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify({ status: result.status, totalUnits, documents: documents.length, checks: checks.length, failures: failures.length, report: path.join(out, 'render-report.json') }));
    if (result.status !== 'PASS') process.exitCode = 1;
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
