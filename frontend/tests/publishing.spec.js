import { test, expect } from "@playwright/test";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const accounts = JSON.parse(
  readFileSync(
    new URL("../../artifacts/demo-accounts.json", import.meta.url),
    "utf8",
  ),
).accounts;
const artifact = (name) =>
  fileURLToPath(new URL(`../../artifacts/${name}`, import.meta.url));

test("browser ingestion, quality, human review, two frozen versions and verified downloads", async ({
  page,
}) => {
  test.setTimeout(90000);
  const errors = [];
  page.on("pageerror", (exception) => errors.push(exception.message));
  const directory = `browser-publication-${randomUUID()}`,
    relative = `${directory}/tone.bin`;
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
  const admin = accounts.find((item) => item.role === "ADMIN");
  await page.goto("/");
  await page.getByLabel("用户名", { exact: true }).fill(admin.username);
  await page.getByLabel("密码", { exact: true }).fill(admin.password);
  const tokenResponse = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/auth/token"),
  );
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "人工审核工作台" }),
  ).toBeVisible();
  const token = (await (await tokenResponse).json()).access_token;
  const headers = { Authorization: `Bearer ${token}` };
  await page.getByRole("button", { name: "数据资产", exact: false }).click();
  await page.getByLabel("相对目录", { exact: true }).fill(directory);
  await page.getByRole("button", { name: "提交扫描", exact: true }).click();
  await expect(page.getByText(/扫描结束：新增 1/)).toBeVisible({
    timeout: 30000,
  });
  await page.getByLabel("搜索文件路径").fill(directory);
  await page.getByRole("button", { name: "查询文件", exact: true }).click();
  await page
    .locator(".asset-center .el-table__body tbody tr")
    .filter({ hasText: relative })
    .click();
  await page.getByLabel("窗口采样数", { exact: true }).fill("64");
  await page.getByLabel("步长采样数", { exact: true }).fill("64");
  await page.getByRole("button", { name: "提交分段任务", exact: true }).click();
  await expect(page.getByText("区间索引已完成：2 个引用。")).toBeVisible({
    timeout: 30000,
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: artifact("phase5-assets.png"),
    fullPage: true,
  });
  const raw = (
    await (
      await page.request.get(`/api/v1/files?q=${directory}`, { headers })
    ).json()
  ).items[0];
  const segments = (
    await (
      await page.request.get(`/api/v1/segments?raw_file_id=${raw.id}`, {
        headers,
      })
    ).json()
  ).items;
  const short = segments[0].id.slice(0, 8);
  await page
    .getByRole("button", { name: "查看片段与预测", exact: true })
    .click();
  const segmentRow = page
    .locator(".el-table__body tbody tr")
    .filter({ hasText: short });
  await segmentRow.click();
  await page.getByRole("button", { name: "为所选片段执行质量检查" }).click();
  await expect(
    page.getByText("质量检查已完成，可查看片段的指标。"),
  ).toBeVisible({ timeout: 30000 });
  await segmentRow.click();
  await page.getByRole("button", { name: "为所选片段提交预标注" }).click();
  await expect(
    page.getByText("预标注已完成，请到人工审核工作台查看。"),
  ).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "人工审核", exact: false }).click();
  async function openReview() {
    const row = page.locator(".review-row").filter({ hasText: short });
    const next = page.locator(".review-list .el-pagination .btn-next");
    await expect(page.locator(".review-row").first()).toBeVisible();
    for (
      let index = 0;
      index < 100 && (await row.count()) === 0 && (await next.isEnabled());
      index++
    ) {
      const loaded = page.waitForResponse((response) =>
        response.url().includes("/api/v1/reviews?"),
      );
      await next.click();
      await loaded;
    }
    await row.click();
    await expect(
      page.getByText("Mock · 模拟结果", { exact: true }),
    ).toBeVisible();
  }
  await openReview();
  await expect(page.locator("canvas")).toHaveCount(2);
  await page
    .getByLabel("审核备注")
    .fill("Synthetic browser publication demo; not measured truth");
  await page
    .getByRole("button", { name: "确认当前模型建议", exact: true })
    .click();
  await expect(page.locator(".history-row")).toHaveCount(1);

  const datasetName = `Browser-${randomUUID()}`;
  await page.getByRole("button", { name: "数据集管理", exact: false }).click();
  await page.getByLabel("数据集名称", { exact: true }).fill(datasetName);
  await page
    .getByLabel("数据集说明", { exact: true })
    .fill("Synthetic end-to-end browser example");
  await page.getByRole("button", { name: "创建数据集", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: datasetName, exact: true }),
  ).toBeVisible();
  async function chooseFileAndPublish(version) {
    await page.getByLabel("版本名", { exact: true }).fill(version);
    await page
      .locator(".el-select")
      .filter({
        has: page.getByRole("combobox", { name: "筛选源文件", exact: true }),
      })
      .click();
    await page.getByRole("option", { name: relative, exact: true }).click();
    await page.getByRole("heading", { name: datasetName, exact: true }).click();
    await page.getByRole("button", { name: "预览选择", exact: true }).click();
    await expect(page.locator(".selection-preview")).toBeVisible();
    await expect(
      page.locator(".selection-preview .summary-tiles b").first(),
    ).toHaveText("1");
    await page
      .getByRole("button", { name: "发布不可变版本", exact: true })
      .click();
    await expect(
      page.getByText(
        `版本 ${version} 已封存，共 1 个样本。后续标签修改不会改变此版本。`,
      ),
    ).toBeVisible({ timeout: 30000 });
    const downloadEvent = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "下载 Manifest", exact: true })
      .click();
    const download = await downloadEvent;
    const bytes = readFileSync(await download.path());
    const document = JSON.parse(bytes.toString("utf8"));
    expect(document.dataset.name).toBe(datasetName);
    expect(document.item_count).toBe(1);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(
      (await page.locator(".hash-block").innerText()).trim(),
    );
    return { document, bytes };
  }
  const first = await chooseFileAndPublish("v1.0.0");
  expect(first.document.items[0].annotation.version).toBe(1);
  const revisedLabel =
    first.document.items[0].label === "BPSK" ? "QPSK" : "BPSK";
  await page.getByRole("button", { name: "人工审核", exact: false }).click();
  await page
    .locator(".el-select")
    .filter({
      has: page.getByRole("combobox", { name: "审核状态", exact: true }),
    })
    .click();
  await page.getByRole("option", { name: "已审核", exact: true }).click();
  await openReview();
  await page
    .locator(".el-select")
    .filter({
      has: page.getByRole("combobox", { name: "选择人工标签", exact: true }),
    })
    .click();
  await page.getByRole("option", { name: revisedLabel, exact: true }).click();
  await page.getByRole("button", { name: "保存人工标签", exact: true }).click();
  await expect(page.locator(".history-row")).toHaveCount(2);
  await page.getByRole("button", { name: "数据集管理", exact: false }).click();
  await page.locator(".dataset-row").filter({ hasText: datasetName }).click();
  const second = await chooseFileAndPublish("v1.0.1");
  expect(second.document.items[0].annotation.version).toBe(2);
  expect(second.document.items[0].label).toBe(revisedLabel);
  expect(first.document.items[0].label).not.toBe(revisedLabel);
  const historical = await page.request.get(
    `/api/v1/dataset-versions/${first.document.version.id}/manifest`,
    { headers },
  );
  expect(await historical.body()).toEqual(first.bytes);
  await expect(
    page.locator(".version-list .el-table__body tbody tr"),
  ).toHaveCount(2);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: artifact("phase5-datasets.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
