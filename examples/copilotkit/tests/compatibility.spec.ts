import { expect, test } from "@playwright/test";

// One user journey per backend exercises the actual CopilotKit tool/interrupt loop.
test("chat, state, frontend tool, approval, failure and a fresh thread", async ({
  page,
  request,
}, info) => {
  const framework = info.project.name;
  await expect
    .poll(
      async () => {
        try {
          return (await request.get(`/api/${framework}/healthz`)).status();
        } catch {
          return 0;
        }
      },
      { timeout: 45_000 },
    )
    .toBe(200);
  await page.goto(`/?framework=${framework}`);
  const input = page.getByPlaceholder("Message the Harnest agent…");
  await input.fill("hello from CopilotKit");
  await input.press("Enter");
  await expect(
    page.getByText("Harnest heard: hello from CopilotKit", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("turns")).toHaveText("1");
  await page.getByRole("button", { name: /Browser tool/ }).click();
  await expect(
    page.getByText("Browser applied violet.", { exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("accent")).toHaveText("violet");
  await expect(page.getByTestId("tool-calls")).toHaveText("1");
  await expect(page.getByTestId("turns")).toHaveText("2");
  await page.getByRole("button", { name: /Human approval/ }).click();
  await page.getByRole("button", { name: "Decline publication" }).click();
  await expect(
    page.getByText("Publication declined. No draft was published.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.getByTestId("draft")).toHaveText("Not published");
  await page.getByRole("button", { name: /Human approval/ }).click();
  await page.getByRole("button", { name: "Approve publication" }).click();
  await expect(page.getByTestId("draft")).toHaveText("Published");
  await expect(page.getByTestId("turns")).toHaveText("4");
  await page.getByRole("button", { name: /Error recovery/ }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: /01 Conversation/ }).click();
  await expect(
    page.getByText("Harnest heard: hello", { exact: true }),
  ).toBeVisible();
  await input.fill("slow");
  await input.press("Enter");
  await page.getByRole("button", { name: "Stop run", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Stop run", exact: true }),
  ).toHaveCount(0);
  await input.fill("after cancel");
  await input.press("Enter");
  await expect(
    page.getByText("Harnest heard: after cancel", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Slow request completed.", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "New conversation" }).click();
  await expect(page.getByTestId("turns")).toHaveText("0");
  await expect(page.getByTestId("draft")).toHaveText("Not published");
  await expect(
    page.getByText("Harnest heard: hello", { exact: true }),
  ).toHaveCount(0);
});
