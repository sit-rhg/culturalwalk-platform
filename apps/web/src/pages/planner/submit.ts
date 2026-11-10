/**
 * 规划提交的「一次意图」守卫：双击、回车连按、失败后重试都不要重复排行程。
 * 表单条件没变时复用同一个 Idempotency-Key，后端按幂等键只落一条计划；
 * 改了条件再提交才算新的意图，换新键。
 */

import type { PlanInput } from '@/api/types'

export interface PlanSubmitter {
  /** 开始一次提交；已有请求在飞时返回 null，调用方必须直接忽略这次点击 */
  begin(payload: PlanInput): string | null
  /** 请求结束（成功或失败都算），放开下一次提交 */
  end(): void
}

function randomKey(): string {
  return crypto.randomUUID()
}

export function createPlanSubmitter(newKey: () => string = randomKey): PlanSubmitter {
  let inFlight = false
  let lastSignature = ''
  let lastKey = ''

  function keyFor(payload: PlanInput): string {
    const signature = JSON.stringify(payload)
    if (signature !== lastSignature) {
      lastSignature = signature
      lastKey = newKey()
    }
    return lastKey
  }

  return {
    begin(payload: PlanInput): string | null {
      if (inFlight) return null
      inFlight = true
      return keyFor(payload)
    },
    end(): void {
      inFlight = false
    },
  }
}
