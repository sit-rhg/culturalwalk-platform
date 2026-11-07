import { expect, test, type Page } from '@playwright/test'

/**
 * 任务卡 QA-02 的主链路验收：访客 → 登录 → 投稿 → 审核 → 发布 → 评论。
 *
 * 一条用例走完整条故事：中间产生的草稿、审核任务、已发布路线互为下一步的输入，
 * 拆成多条只会让状态靠 storageState 传来传去，反而更难读。
 * 每一步都用 test.step 标好名字，失败时报告能直接指到断在第几步。
 */

const PASSWORD = 'Demo12345!'
const DEMO_USER = 'demo@demo.local'
const REVIEWER = 'reviewer@demo.local'

/** 每次跑都换标题，避免和历史数据撞在一起，也让断言能精确定位这一次的产物 */
const STAMP = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14)
const DRAFT_TITLE = `QA-02 主链路 ${STAMP}`
const COMMENT_BODY = `QA-02 评论 ${STAMP}`

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function navLink(page: Page, label: string) {
  return page.getByRole('navigation', { name: '主导航' }).getByRole('link', { name: label, exact: true })
}

/** 登录页的「演示账号」按钮会一键填好表单，这里点按钮再提交，不去手打邮箱密码 */
async function loginWithDemoAccount(page: Page, username: string): Promise<void> {
  await page.goto('/login')
  const demoButton = page.getByRole('button', { name: new RegExp(escapeRegExp(username)) })
  await expect(demoButton).toBeVisible()
  await demoButton.click()
  await expect(page.getByLabel('用户名')).toHaveValue(username)
  await page.locator('form').getByRole('button', { name: '登录', exact: true }).click()
  // 登录成功会 replace 回发现页；发现页的标题出现即代表这次登录生效了
  await expect(page.getByRole('heading', { name: '发现', exact: true })).toBeVisible()
}

test('访客到评论：一条路线从提交到发布的全流程', async ({ page }) => {
  await test.step('访客打开 /discover，能看到路线卡片标题', async () => {
    await page.goto('/discover')
    // 前端没有 /discover 路由，会客户端重定向到「发现」页（/）
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { name: '发现', exact: true })).toBeVisible()

    const cards = page.locator('main a[href^="/routes/"]')
    await expect(cards.first()).toBeVisible()
    const firstTitle = cards.first().getByRole('heading', { level: 3 })
    await expect(firstTitle).toBeVisible()
    await expect(firstTitle).not.toHaveText('')
  })

  await test.step('用 demo@demo.local 登录，「我的」页显示用户名', async () => {
    await loginWithDemoAccount(page, DEMO_USER)
    await navLink(page, '我的').click()
    await expect(page.getByRole('heading', { name: '我的主页', exact: true })).toBeVisible()
    await expect(page.getByText(DEMO_USER, { exact: true })).toBeVisible()
  })

  await test.step('在 /authoring 新建草稿，加 2 个节点后保存', async () => {
    await navLink(page, '创作').click()
    await expect(page.getByRole('heading', { name: '创作路线', exact: true })).toBeVisible()

    await page.getByRole('button', { name: '新建草稿', exact: true }).click()
    await page.getByPlaceholder('例如：城南旧事：第一师范与白沙井').fill(DRAFT_TITLE)
    await page.getByRole('button', { name: '创建', exact: true }).click()

    // 创建成功后自动进入编辑器，标题在编辑区头部可见
    await expect(page.getByText(DRAFT_TITLE, { exact: true }).first()).toBeVisible()

    for (const index of ['第一个', '第二个']) {
      await page.getByRole('button', { name: '添加节点', exact: true }).click()
      const addCard = page.locator('.add-card')
      await expect(addCard).toBeVisible()
      // 地点下拉的第一个 option 是 disabled 的占位项，从第 2 项起才是真点位
      await addCard.getByLabel('地点').selectOption({ index: 1 })
      await addCard.getByRole('button', { name: '加入末尾', exact: true }).click()
      await expect(addCard).toBeHidden()
      const nodeCount = index === '第一个' ? 1 : 2
      await expect(page.locator('ol.nodes').getByRole('listitem')).toHaveCount(nodeCount)
    }

    await page.getByRole('button', { name: '保存', exact: true }).click()
    await expect(page.getByText('已保存', { exact: true })).toBeVisible()
    await expect(page.locator('ol.nodes').getByRole('listitem')).toHaveCount(2)
  })

  await test.step('在 /submit 提交审核，状态徽章从「编辑中」变成「审核中」', async () => {
    await navLink(page, '投稿').click()
    await expect(page.getByRole('heading', { name: '我的路线', exact: true })).toBeVisible()

    const draftCard = page.locator('article.draft-card').filter({ hasText: DRAFT_TITLE })
    await expect(draftCard).toBeVisible()
    await expect(draftCard.getByText('编辑中', { exact: true })).toBeVisible()

    await draftCard.getByRole('button', { name: '提交审核', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('提交审核', { exact: true }).first()).toBeVisible()
    await dialog.getByRole('button', { name: '确认提交', exact: true }).click()

    await expect(page.getByRole('status')).toContainText('已提交')
    // 提交后：状态徽章和操作按钮都变成「审核中」（按钮此时是禁用的占位）
    await expect(draftCard.locator('.badge')).toHaveText('审核中')
    await expect(draftCard.getByRole('button', { name: '审核中', exact: true })).toBeDisabled()
  })

  await test.step('退出后换 reviewer@demo.local 登录，进入 /admin', async () => {
    await page.goto('/login')
    await expect(page.getByText('你已登录为')).toBeVisible()
    await page.getByRole('button', { name: '退出登录', exact: true }).click()
    await loginWithDemoAccount(page, REVIEWER)

    await page.goto('/admin')
    await expect(page.getByRole('heading', { name: '审核后台', exact: true })).toBeVisible()
  })

  await test.step('领取任务、展开版本对照、点「通过」', async () => {
    // 队列行是 role="button" 的 li，可读名里带着「路线标题 · 作者」，用它定位本次任务
    const taskRow = page.getByRole('button', { name: new RegExp(escapeRegExp(DRAFT_TITLE)) })
    await expect(taskRow).toBeVisible({ timeout: 20_000 })
    await expect(taskRow.getByText('待领取', { exact: true })).toBeVisible()

    await taskRow.getByRole('button', { name: '领取', exact: true }).click()
    await expect(taskRow.getByText('审核中', { exact: true })).toBeVisible()

    // 键盘回车展开与点击整行等价，避免误点到行内按钮
    await taskRow.press('Enter')
    await expect(page.getByText(`版本对照 · ${DRAFT_TITLE}`, { exact: true })).toBeVisible()
    await expect(page.getByText('上一版', { exact: true })).toBeVisible()
    await expect(page.getByText('本次', { exact: true })).toBeVisible()

    await page.getByRole('button', { name: '通过', exact: true }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText('通过后这次提交进入发布流程')).toBeVisible()
    await dialog.getByRole('button', { name: '确认通过', exact: true }).click()

    await expect(page.getByText(/已通过，任务/)).toBeVisible()
    await expect(taskRow.getByText('已完成', { exact: true })).toBeVisible()
  })

  await test.step('点「发布上线」，提示成功', async () => {
    const taskRow = page.getByRole('button', { name: new RegExp(escapeRegExp(DRAFT_TITLE)) })
    await taskRow.getByRole('button', { name: '发布上线', exact: true }).click()
    await expect(page.getByText('已发布上线，发现页现在能看到它', { exact: true })).toBeVisible()
  })

  await test.step('回 /discover，新路线在列表里；打开详情发表评论', async () => {
    await navLink(page, '发现').click()
    await expect(page.getByRole('heading', { name: '发现', exact: true })).toBeVisible()

    const newCard = page
      .locator('main a[href^="/routes/"]')
      .filter({ hasText: DRAFT_TITLE })
      .first()
    await expect(newCard).toBeVisible()

    await newCard.click()
    await expect(page.getByRole('heading', { name: DRAFT_TITLE, exact: true })).toBeVisible()

    await page.getByPlaceholder('路况、门票、讲解时间都可以写').fill(COMMENT_BODY)
    await page.getByRole('button', { name: '发表评论', exact: true }).click()
    await expect(page.getByText('评论已发表', { exact: true })).toBeVisible()

    const comment = page.locator('li.comment').filter({ hasText: COMMENT_BODY })
    await expect(comment).toBeVisible()
    await expect(comment).toContainText(REVIEWER)
  })
})
