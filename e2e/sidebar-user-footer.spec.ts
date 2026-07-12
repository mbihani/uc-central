import { test, expect } from "@playwright/test";

test.describe("Sidebar user footer", () => {
  test("loads app and shows user footer with dropdown", async ({ page }) => {
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
    // Wait for user data to load (skeleton replaced by actual content with user name)
    const userFooter = sidebarFooter.locator('[data-sidebar="menu-button"]');
    await expect(userFooter).toBeVisible({ timeout: 5000 });
    // Wait for user identity to appear (not skeleton)
    await expect(userFooter.locator("span.font-medium")).toBeVisible({
      timeout: 15000,
    });
    await userFooter.click();

    // 4. Verify dropdown opens with auth options
    const signInOption = page.getByRole("menuitem", {
      name: /Sign In with PAT|Switch User/,
    });
    await expect(signInOption).toBeVisible({ timeout: 5000 });

    // 5. Take screenshot of dropdown
    await page.screenshot({
      path: "e2e/screenshots/user-dropdown.png",
    });

    // Verify dropdown shows user identity
    const dropdownContent = page.locator('[role="menu"]');
    await expect(dropdownContent).toBeVisible();
    await expect(dropdownContent).toContainText(/Sign In with PAT|Switch User/);
  });
});
