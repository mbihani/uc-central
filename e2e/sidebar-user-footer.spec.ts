import { test, expect } from "@playwright/test";

test.describe("Sidebar user footer", () => {
  test("loads app and shows the proxied user in the footer", async ({
    page,
  }) => {
    // 1. Navigate and verify app loads
    await page.goto("/", { waitUntil: "load", timeout: 15000 });

    // 2. Take screenshot of initial page
    await page.screenshot({
      path: "e2e/screenshots/initial-page.png",
      fullPage: true,
    });

    // 3. Find the user footer in the sidebar footer (bottom-left)
    const sidebarFooter = page.locator('[data-sidebar="footer"]');
    await expect(sidebarFooter).toBeVisible({ timeout: 15000 });
    const userFooter = sidebarFooter.locator('[data-sidebar="menu-button"]');
    await expect(userFooter).toBeVisible({ timeout: 5000 });

    // 4. The footer shows the identity injected by the Databricks Apps proxy.
    //    There is no PAT sign-in / switch-user UI: the app never stores or
    //    prompts for a token, so no auth dropdown should exist.
    await expect(userFooter.locator("span.font-medium")).toBeVisible({
      timeout: 15000,
    });
    await userFooter.click();
    await expect(
      page.getByRole("menuitem", { name: /Sign In with PAT|Switch User/ }),
    ).toHaveCount(0);

    // 5. Take screenshot of the footer
    await page.screenshot({
      path: "e2e/screenshots/user-footer.png",
    });
  });
});
