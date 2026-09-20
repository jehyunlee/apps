const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const base = process.env.APPS_URL || 'https://app.jehyunlee.dev/';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [], externalErrors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    if (response.status() < 400) return;
    (new URL(response.url()).origin === new URL(base).origin ? errors : externalErrors).push(response.status() + ' ' + response.url());
  });
  try {
    await page.goto(base);
    assert.equal(await page.title(), "Jehyun's Apps");
    assert.equal(await page.locator('h1').textContent(), "Jehyun's Apps");
    await page.locator('a[href="history_korea/"]').click();
    assert.equal(await page.locator('ol li a').count(), 5);
    for (let i = 1; i <= 5; i++) {
      const response = await page.goto(new URL('history_korea/' + i + '/', base).href);
      assert.equal(response.status(), 200);
      assert.match(await page.title(), /수안정안 역사이야기/);
      const localImages = await page.locator('img').evaluateAll(images => images.map(img => img.src).filter(src => new URL(src).origin === location.origin));
      for (let start = 0; start < localImages.length; start += 8) {
        await Promise.all(localImages.slice(start, start + 8).map(async url => {
          const response = await page.request.get(url);
          assert.equal(response.status(), 200, url);
        }));
      }
      const first = page.locator('.quiz-box').first().locator('.quiz-item[onclick*="true"]').first();
      await first.click();
      assert.ok((await first.getAttribute('class')).includes('correct'));
      const wrong = page.locator('.quiz-box').nth(1).locator('.quiz-item[onclick*="false"]').first();
      await wrong.click();
      assert.ok((await wrong.getAttribute('class')).includes('wrong'));
      const siblingLinks = await page.locator('a[href^="../"]').evaluateAll(links => [...new Set(links.map(link => link.href))]);
      for (const url of siblingLinks) assert.equal((await page.request.get(url)).status(), 200, url);
      console.log('PASS volume', i, ': images', localImages.length, ', quizzes and cross-volume links');
    }
    assert.deepEqual(errors, []);
    if (externalErrors.length) console.warn('External source resource failures:', [...new Set(externalErrors)]);
    console.log('PASS collection and Jehyun title.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
