import { test, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

// Explicit local smoke test: uses synthetic IQ + private generated demo credentials.
const accounts = JSON.parse(
  readFileSync(
    new URL("../../artifacts/demo-accounts.json", import.meta.url),
    "utf8",
  ),
).accounts;
const screenshot = fileURLToPath(
  new URL("../../artifacts/phase4-review.png", import.meta.url),
);

async function login(page, role) {
  const account = accounts.find((item) => item.role === role);
  await page.goto("/");
  await page.getByLabel("用户名", { exact: true }).fill(account.username);
  await page.getByLabel("密码", { exact: true }).fill(account.password);
  const loggedIn = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/auth/token"),
  );
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "人工审核工作台" }),
  ).toBeVisible();
  return (await (await loggedIn).json()).access_token;
}

test("real browser inference, IQ charts, review history and stale-draft conflict", async ({
  page,
  browser,
}) => {
  const errors = [];
  page.on("pageerror", (exception) => errors.push(exception.message));
  const token = await login(page, "ADMIN");
  const headers = { Authorization: `Bearer ${token}` };
  const directory = `browser-e2e-${randomUUID()}`;
  const folder = fileURLToPath(
    new URL(`../../data/${directory}/`, import.meta.url),
  );
  mkdirSync(folder);
  const iq = Buffer.alloc(128 * 8);
  for (let index = 0; index < 128; index++) {
    iq.writeFloatLE(Math.cos(index / 10), index * 8);
    iq.writeFloatLE(Math.sin(index / 10), index * 8 + 4);
  }
  writeFileSync(`${folder}/tone.bin`, iq);
  writeFileSync(
    `${folder}/tone.bin.iq.json`,
    JSON.stringify({
      format: "BIN",
      dtype: "float32",
      byte_order: "little",
      interleaving: "IQ",
      sample_rate_hz: 1000000,
      center_frequency_hz: 100000000,
    }),
  );
  writeFileSync(`${folder}/tone.bin.done`, "");
  async function run(body) {
    const response = await page.request.post("/api/v1/jobs", {
      headers: { ...headers, "Idempotency-Key": randomUUID() },
      data: body,
    });
    expect(response.status()).toBe(202);
    const submitted = await response.json();
    let job;
    await expect
      .poll(
        async () => {
          job = await (
            await page.request.get(`/api/v1/jobs/${submitted.id}`, { headers })
          ).json();
          return job.status;
        },
        { timeout: 30000 },
      )
      .toBe("SUCCEEDED");
    return job.result;
  }
  const scan = await run({ job_type: "SCAN", directory });
  const indexed = await run({
    job_type: "SEGMENT",
    raw_file_id: scan.files[0].id,
    config: { window_samples: 64 },
  });
  const segmentId = indexed.segment_ids[0];
  const segmentShort = segmentId.slice(0, 8);
  async function openFreshReview(target) {
    const row = target.locator(".review-row").filter({ hasText: segmentShort });
    const next = target.locator(".el-pagination .btn-next");
    for (
      let attempt = 0;
      attempt < 100 && (await row.count()) === 0 && (await next.isEnabled());
      attempt++
    ) {
      const loaded = target.waitForResponse((response) =>
        response.url().includes("/api/v1/reviews?"),
      );
      await next.click();
      await loaded;
    }
    await expect(row).toHaveCount(1);
    await row.click();
  }
  await page.getByRole("button", { name: "片段与预测", exact: false }).click();
  await expect(page.locator(".el-table__body tbody tr").first()).toBeVisible();
  const nextPage = page.locator(".el-pagination .btn-next");
  while (await nextPage.isEnabled()) {
    const loaded = page.waitForResponse((response) =>
      response.url().includes("/api/v1/segments?"),
    );
    await nextPage.click();
    await loaded;
  }
  await page
    .locator(".el-table__body tbody tr")
    .filter({ hasText: segmentShort })
    .click();
  const completed = page.waitForResponse(
    (response) =>
      response.url().includes("/api/v1/jobs/") &&
      response.status() === 200 &&
      response.request().method() === "GET",
  );
  await page.getByRole("button", { name: "为所选片段提交预标注" }).click();
  await completed;
  await expect(
    page.getByText("预标注已完成，请到人工审核工作台查看。"),
  ).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "人工审核", exact: false }).click();
  await openFreshReview(page);
  await expect(
    page.getByText("Mock · 模拟结果", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("canvas")).toHaveCount(2);
  // A second real browser session opens the same initial version before either writes.
  const context = await browser.newContext({
    baseURL: process.env.RF_E2E_URL || "http://127.0.0.1:18080",
    viewport: { width: 1440, height: 1000 },
  });
  const other = await context.newPage();
  await login(other, "REVIEWER");
  await openFreshReview(other);
  await expect(
    other.getByText("Mock · 模拟结果", { exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("审核备注")
    .fill("Browser smoke on synthetic IQ; not a measured truth label");
  await page
    .getByRole("button", { name: "确认当前模型建议", exact: true })
    .click();
  await expect(
    page.getByText("人工结论已保存，历史记录已追加。"),
  ).toBeVisible();
  await expect(page.locator(".history-row")).toHaveCount(1);
  await other.getByLabel("审核备注").fill("Draft must survive a conflict");
  await other.getByRole("button", { name: "标记未知", exact: true }).click();
  await expect(
    other.getByText(
      "这份样本已被另一位审核者修改。你的草稿已保留，请重新加载后再决定。",
    ),
  ).toBeVisible();
  await expect(other.getByLabel("审核备注")).toHaveValue(
    "Draft must survive a conflict",
  );
  await expect(
    other.getByRole("button", { name: "标记未知", exact: true }),
  ).toBeDisabled();
  await other.getByRole("button", { name: "重新加载并放弃当前草稿" }).click();
  await expect(other.getByLabel("审核备注")).toHaveValue("");
  await expect(other.locator(".history-row")).toHaveCount(1);
  await page.screenshot({ path: screenshot, fullPage: true });
  await other.getByRole("button", { name: "退出登录", exact: true }).click();
  await expect(
    other.getByRole("heading", { name: "登录工作台" }),
  ).toBeVisible();
  await context.close();
  expect(errors).toEqual([]);
});
