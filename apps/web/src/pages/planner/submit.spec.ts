import { describe, expect, it } from 'vitest'

import type { PlanInput } from '@/api/types'
import { defaultForm, toPlanInput } from '@/modules/planner-form/plan-form'

import { createPlanSubmitter } from './submit'

function payload(overrides: Partial<Parameters<typeof toPlanInput>[0]> = {}): PlanInput {
  return toPlanInput({ ...defaultForm(), ...overrides })
}

function keyGen(): () => string {
  let count = 0
  return () => 'key-' + ++count
}

describe('createPlanSubmitter', () => {
  it('连续两次点击只发一次请求，第二次点击被丢弃', async () => {
    const submitter = createPlanSubmitter(keyGen())
    const sent: PlanInput[] = []
    let release: () => void = () => {}
    const request = (input: PlanInput): Promise<void> =>
      new Promise((resolve) => {
        sent.push(input)
        release = resolve
      })

    /** 与页面里 startPlan 同构：先过守卫，再发请求，finally 放开守卫 */
    async function click(input: PlanInput): Promise<void> {
      const key = submitter.begin(input)
      if (!key) return
      try {
        await request(input)
      } finally {
        submitter.end()
      }
    }

    const first = click(payload())
    const second = click(payload())
    expect(sent).toHaveLength(1)

    release()
    await Promise.all([first, second])

    // 请求结束后再点一次才算新的提交
    const third = click(payload())
    expect(sent).toHaveLength(2)
    release()
    await third
  })

  it('同一次提交意图复用同一个幂等键，改了条件才换新键', () => {
    const submitter = createPlanSubmitter(keyGen())

    const first = submitter.begin(payload())
    submitter.end()
    const sameIntent = submitter.begin(payload())
    submitter.end()

    expect(first).toBe('key-1')
    expect(sameIntent).toBe(first)

    const changed = submitter.begin(payload({ days: 3 }))
    submitter.end()
    expect(changed).toBe('key-2')
  })
})
