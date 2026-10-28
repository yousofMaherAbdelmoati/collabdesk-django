const { test, expect } = require("@playwright/test");

const localPassword = process.env.E2E_DEMO_PASSWORD || "local-demo-password";

async function login(page, username) {
  await page.goto("/");
  await page.locator("#username").fill(username);
  await page.locator("#password").fill(localPassword);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator("#app-view")).toBeVisible();
  await expect(page.locator("#ticket-list")).toBeVisible();
}

async function openTicket(page, title) {
  const card = page.locator(".ticket").filter({ hasText: title });
  await expect(card).toBeVisible();
  await card.locator(".ticket-main").click();
  await expect(page.locator("#conversation-panel")).toBeVisible();
  await expect(page.locator("#connection-state")).toContainText("Live connection");
}

test("customer and agent complete the live support workflow", async ({ browser }, testInfo) => {
  const runId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const title = `DJ-011 fictional ticket ${runId}`;
  const description = `Fictional browser E2E request ${runId}; contains no customer data.`;
  const customerMessage = `Customer live message ${runId}`;
  const agentMessage = `Agent live reply ${runId}`;
  const customerContext = await browser.newContext();
  const agentContext = await browser.newContext();
  const adminContext = await browser.newContext();
  const customer = await customerContext.newPage();
  const agent = await agentContext.newPage();
  const admin = await adminContext.newPage();

  try {
    await login(customer, "demo-customer");
    await customer.locator("#ticket-title").fill(title);
    await customer.locator("#ticket-description").fill(description);
    await customer.getByRole("button", { name: "Create ticket" }).click();
    const customerCard = customer.locator(".ticket").filter({ hasText: title });
    await expect(customerCard).toBeVisible();
    const ticketId = Number((await customerCard.locator(".ticket-meta").innerText()).match(/#(\d+)/)[1]);

    await login(agent, "demo-agent");
    const agentCard = agent.locator(".ticket").filter({ hasText: title });
    await expect(agentCard).toBeVisible();

    // Before claiming, the agent can discover the open ticket but is not a
    // conversation participant. This browser-session request is rejected by
    // the server before any conversation socket may be authorized.
    const denied = await agent.evaluate(async (id) => {
      const response = await fetch(`/api/tickets/${id}/messages`);
      return { status: response.status, body: await response.json() };
    }, ticketId);
    expect(denied.status).toBe(403);
    expect(denied.body.code).toBe("participant_forbidden");

    await agentCard.getByRole("button", { name: "Claim" }).click();
    await expect(agent.locator(".ticket").filter({ hasText: title })).toContainText("claimed");

    await openTicket(customer, title);
    await openTicket(agent, title);
    await customer.locator("#message-body").fill(customerMessage);
    await customer.getByRole("button", { name: "Send" }).click();
    await expect(agent.locator("#messages")).toContainText(customerMessage);
    await agent.locator("#message-body").fill(agentMessage);
    await agent.getByRole("button", { name: "Send" }).click();
    await expect(customer.locator("#messages")).toContainText(agentMessage);

    // A page reload makes a new browser socket; selecting the ticket again
    // asserts durable message history is recovered after reconnect.
    await agent.reload();
    await expect(agent.locator("#ticket-list")).toBeVisible();
    await openTicket(agent, title);
    await expect(agent.locator("#messages")).toContainText(customerMessage);
    await expect(agent.locator("#messages")).toContainText(agentMessage);

    await customer.getByRole("button", { name: "Assistant" }).click();
    await customer.getByRole("button", { name: "Start demo stream" }).click();
    await expect(customer.locator("#assistant-cancel")).toBeVisible();
    await expect(customer.locator("#assistant-output")).toContainText("Demo ");
    await customer.getByRole("button", { name: "Cancel stream" }).click();
    await expect(customer.locator("#assistant-output")).toContainText("Stream cancelled locally.");

    await login(admin, "demo-admin");
    await admin.getByRole("button", { name: "Reports & cache" }).click();
    await expect(admin.locator("#cache-display")).toContainText("tickets in this view");
    await admin.getByRole("button", { name: "Request report" }).click();
    await expect(admin.locator("#report-status")).toContainText(/Report #\d+:/);
    await expect(admin.locator("#report-result")).toBeVisible({ timeout: 45_000 });
    await expect(admin.locator("#report-result")).toContainText("time_buckets");

    // This screenshot is generated only after the report output is visibly
    // rendered by the real browser and is retained as a test artifact.
    const screenshot = testInfo.outputPath("dj-011-completed-report.png");
    await admin.screenshot({ path: screenshot, fullPage: true });
    await testInfo.attach("completed-report", { path: screenshot, contentType: "image/png" });
  } finally {
    await Promise.all([customerContext.close(), agentContext.close(), adminContext.close()]);
  }
});
